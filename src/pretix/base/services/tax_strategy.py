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
Tax calculation strategies for TaxRule.

Implements the Strategy pattern to consolidate tax computation logic
that was previously scattered across the codebase. Each strategy
encapsulates a specific tax regime (standard, EU reverse charge,
custom rules) behind a common interface.

Usage::

    strategy = get_tax_strategy(tax_rule)
    is_applicable = strategy.is_tax_applicable(
        tax_rule, invoice_address
    )
    rate = strategy.get_tax_rate(tax_rule, invoice_address)
    code = strategy.get_tax_code(tax_rule, invoice_address)
    is_rc = strategy.is_reverse_charge(tax_rule, invoice_address)
"""
import abc
from decimal import Decimal

from pretix.base.models.tax import is_eu_country


class TaxCalculationStrategy(abc.ABC):
    """
    Abstract base class for tax calculation strategies.

    Each concrete strategy implements the decision logic for
    a specific tax regime (standard VAT, EU reverse charge,
    custom rules, etc.).
    """

    @abc.abstractmethod
    def is_tax_applicable(self, tax_rule, invoice_address):
        """
        Determine whether tax should be applied.

        :param tax_rule: The ``TaxRule`` instance.
        :param invoice_address: The customer's ``InvoiceAddress``,
            or ``None``.
        :returns: ``True`` if the tax rate should be charged.
        :raises TaxRule.SaleNotAllowed: If the sale is blocked.
        """

    @abc.abstractmethod
    def get_tax_rate(self, tax_rule, invoice_address):
        """
        Return the effective tax rate for this context.

        :returns: ``Decimal`` tax rate percentage.
        :raises TaxRule.SaleNotAllowed: If the sale is blocked.
        """

    @abc.abstractmethod
    def get_tax_code(self, tax_rule, invoice_address):
        """
        Return the effective tax code for this context.

        :returns: A string tax code or ``None``.
        """

    @abc.abstractmethod
    def is_reverse_charge(self, tax_rule, invoice_address):
        """
        Determine whether reverse charge applies.

        :returns: ``True`` if the transaction is reverse-charged.
        """

    @abc.abstractmethod
    def get_invoice_text(self, tax_rule, invoice_address):
        """
        Return any special invoice text for this tax context.

        :returns: A string or ``None``.
        """


class StandardTaxStrategy(TaxCalculationStrategy):
    """
    Standard VAT strategy — always applies the configured rate.

    Used when ``eu_reverse_charge`` is ``False`` and no custom
    rules are configured.
    """

    def is_tax_applicable(self, tax_rule, invoice_address):
        return True

    def get_tax_rate(self, tax_rule, invoice_address):
        return Decimal(tax_rule.rate)

    def get_tax_code(self, tax_rule, invoice_address):
        return tax_rule.code

    def is_reverse_charge(self, tax_rule, invoice_address):
        return False

    def get_invoice_text(self, tax_rule, invoice_address):
        return None


class EUReverseChargeStrategy(TaxCalculationStrategy):
    """
    EU reverse charge strategy (legacy).

    Zeroes out VAT for business customers in other EU countries
    who have a validated VAT ID, and for all customers outside
    the EU.
    """

    def _is_reverse_charge_applicable(
        self, tax_rule, invoice_address
    ):
        if not invoice_address or not invoice_address.country:
            return False

        if not is_eu_country(invoice_address.country):
            return False

        if invoice_address.country == tax_rule.home_country:
            return False

        if (
            invoice_address.is_business
            and invoice_address.vat_id
            and invoice_address.vat_id_validated
        ):
            return True

        return False

    def is_tax_applicable(self, tax_rule, invoice_address):
        if not invoice_address or not invoice_address.country:
            return True

        if not is_eu_country(invoice_address.country):
            return False

        if invoice_address.country == tax_rule.home_country:
            return True

        if (
            invoice_address.is_business
            and invoice_address.vat_id
            and invoice_address.vat_id_validated
        ):
            return False

        return True

    def get_tax_rate(self, tax_rule, invoice_address):
        if not self.is_tax_applicable(tax_rule, invoice_address):
            return Decimal('0.00')
        return Decimal(tax_rule.rate)

    def get_tax_code(self, tax_rule, invoice_address):
        if not invoice_address or not invoice_address.country:
            return tax_rule.code

        if not is_eu_country(invoice_address.country):
            return "O"

        if invoice_address.country == tax_rule.home_country:
            return tax_rule.code

        if (
            invoice_address.is_business
            and invoice_address.vat_id
            and invoice_address.vat_id_validated
        ):
            return "AE"

        return tax_rule.code

    def is_reverse_charge(self, tax_rule, invoice_address):
        return self._is_reverse_charge_applicable(
            tax_rule, invoice_address
        )

    def get_invoice_text(self, tax_rule, invoice_address):
        from django.utils.translation import pgettext

        if self.is_reverse_charge(tax_rule, invoice_address):
            if is_eu_country(invoice_address.country):
                return pgettext(
                    "invoice",
                    "Reverse Charge: According to Article 194, "
                    "196 of Council Directive 2006/112/EEC, VAT "
                    "liability rests with the service recipient.",
                )
            else:
                return pgettext(
                    "invoice",
                    "VAT liability rests with the service "
                    "recipient.",
                )
        return None


class CustomRulesStrategy(TaxCalculationStrategy):
    """
    Custom rules strategy.

    Delegates to the JSON-based custom rules configured on the
    ``TaxRule``. This strategy handles all custom rule actions
    including ``vat``, ``reverse``, ``block``, and
    ``require_approval``.
    """

    def _get_rule(self, tax_rule, invoice_address):
        return tax_rule.get_matching_rule(invoice_address)

    def is_tax_applicable(self, tax_rule, invoice_address):
        rule = self._get_rule(tax_rule, invoice_address)
        action = rule.get('action', 'vat')
        if action == 'block':
            raise tax_rule.SaleNotAllowed()
        return action in ('vat', 'require_approval')

    def get_tax_rate(self, tax_rule, invoice_address):
        rule = self._get_rule(tax_rule, invoice_address)
        action = rule.get('action', 'vat')
        if action == 'block':
            raise tax_rule.SaleNotAllowed()
        if (
            action in ('vat', 'require_approval')
            and rule.get('rate') is not None
        ):
            return Decimal(rule.get('rate'))
        if not self.is_tax_applicable(tax_rule, invoice_address):
            return Decimal('0.00')
        return Decimal(tax_rule.rate)

    def get_tax_code(self, tax_rule, invoice_address):
        rule = self._get_rule(tax_rule, invoice_address)
        if rule.get("code"):
            return rule["code"]
        if rule.get("action", "vat") == "reverse":
            return "AE"
        return tax_rule.code

    def is_reverse_charge(self, tax_rule, invoice_address):
        rule = self._get_rule(tax_rule, invoice_address)
        return rule['action'] == 'reverse'

    def get_invoice_text(self, tax_rule, invoice_address):
        from i18nfield.strings import LazyI18nString

        rule = self._get_rule(tax_rule, invoice_address)
        t = rule.get('invoice_text', {})
        if t and any(v for v in t.values()):
            return str(LazyI18nString(t))
        return None


def get_tax_strategy(tax_rule):
    """
    Factory function that returns the appropriate
    ``TaxCalculationStrategy`` for the given ``TaxRule``.

    :param tax_rule: A ``TaxRule`` instance.
    :returns: A ``TaxCalculationStrategy`` instance.
    """
    if tax_rule.has_custom_rules:
        return CustomRulesStrategy()
    if tax_rule.eu_reverse_charge:
        return EUReverseChargeStrategy()
    return StandardTaxStrategy()

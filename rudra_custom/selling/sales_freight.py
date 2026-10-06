import json

import frappe
from frappe import _
from frappe.utils import cint, flt

from erpnext.accounts.utils import get_account_currency


SUPPORTED_DOCTYPES = ("Quotation", "Sales Order", "Sales Invoice")
FREIGHT_DESCRIPTION = "Freight & Forwarding Charges"


def before_validate(doc, method=None):
	if doc.doctype not in SUPPORTED_DOCTYPES:
		return

	normalize_sales_freight(doc, recalculate=False)


def validate(doc, method=None):
	if doc.doctype not in SUPPORTED_DOCTYPES:
		return

	normalize_sales_freight(doc, recalculate=True)


@frappe.whitelist()
def recalculate_sales_freight(doc):
	if isinstance(doc, str):
		doc = frappe.get_doc(json.loads(doc))
	else:
		doc = frappe.get_doc(doc)

	if doc.doctype not in SUPPORTED_DOCTYPES:
		frappe.throw(_("Freight recalculation is not supported for {0}").format(doc.doctype))

	normalize_sales_freight(doc, recalculate=True)
	clear_freight_calculation_flags(doc)
	return doc.as_dict()


def normalize_sales_freight(doc, recalculate=False):
	if getattr(doc.flags, "in_custom_freight_calculation", False):
		return

	doc.flags.in_custom_freight_calculation = True
	try:
		remove_freight_tax_rows(doc, migrate_existing=True)
		set_freight_defaults_and_totals(doc)
		validate_freight(doc)

		if recalculate:
			if flt(doc.get("custom_freight_and_forwarding_charges")):
				calculate_with_temporary_freight_row(doc)
			else:
				doc.calculate_taxes_and_totals()
			set_freight_defaults_and_totals(doc)
	finally:
		doc.flags.in_custom_freight_calculation = False


def set_freight_defaults_and_totals(doc):
	freight = flt(
		doc.get("custom_freight_and_forwarding_charges"),
		doc.precision("custom_freight_and_forwarding_charges"),
	)
	conversion_rate = flt(doc.get("conversion_rate")) or 1
	base_freight = flt(
		freight * conversion_rate,
		doc.precision("base_custom_freight_and_forwarding_charges"),
	)

	doc.custom_freight_and_forwarding_charges = freight
	doc.base_custom_freight_and_forwarding_charges = base_freight
	doc.custom_taxable_value_with_freight = flt(
		flt(doc.get("net_total")) + freight,
		doc.precision("custom_taxable_value_with_freight"),
	)
	doc.base_custom_taxable_value_with_freight = flt(
		flt(doc.get("base_net_total")) + base_freight,
		doc.precision("base_custom_taxable_value_with_freight"),
	)

	if freight and not doc.get("custom_freight_account"):
		doc.custom_freight_account = get_default_freight_account(doc.company)


def validate_freight(doc):
	freight = flt(doc.get("custom_freight_and_forwarding_charges"))
	if freight < 0 and not cint(doc.get("is_return")):
		frappe.throw(_("Freight & Forwarding Charges cannot be negative."))

	if freight and doc.doctype == "Sales Invoice" and not doc.get("custom_freight_account"):
		frappe.throw(
			_(
				"Please select Freight & Forwarding Account before saving/submitting Sales Invoice."
			)
		)


def calculate_with_temporary_freight_row(doc):
	freight = flt(
		doc.get("custom_freight_and_forwarding_charges"),
		doc.precision("custom_freight_and_forwarding_charges"),
	)
	if not freight:
		return

	if not cint(doc.get("custom_freight_taxable")):
		# Freight is separate and non-taxable: standard tax calculation remains on item net total.
		doc.calculate_taxes_and_totals()
		add_non_taxable_freight_to_totals(doc)
		return

	if not doc.get("custom_freight_account"):
		frappe.throw(_("Please select Freight & Forwarding Account."))

	remove_freight_tax_rows(doc)
	doc.calculate_taxes_and_totals()
	allocate_freight_to_items_for_tax_calculation(doc, freight)
	doc.calculate_taxes_and_totals()
	restore_item_amounts_after_freight_tax_calculation(doc)
	replace_net_totals_after_freight_tax_calculation(doc)
	set_freight_defaults_and_totals(doc)


def add_non_taxable_freight_to_totals(doc):
	freight = flt(doc.get("custom_freight_and_forwarding_charges"))
	base_freight = flt(doc.get("base_custom_freight_and_forwarding_charges"))

	doc.grand_total = flt(doc.grand_total + freight, doc.precision("grand_total"))
	doc.base_grand_total = flt(
		doc.base_grand_total + base_freight,
		doc.precision("base_grand_total"),
	)

	if doc.meta.get_field("rounded_total") and hasattr(doc, "set_rounded_total"):
		doc.set_rounded_total()


def allocate_freight_to_items_for_tax_calculation(doc, freight):
	eligible_items = [
		item for item in doc.get("items") if flt(item.get("net_amount")) and flt(item.get("qty"))
	]
	if not eligible_items:
		return

	doc.flags.custom_freight_original_item_values = []
	doc.flags.custom_freight_original_totals = {
		"total": flt(doc.get("total")),
		"base_total": flt(doc.get("base_total")),
		"net_total": flt(doc.get("net_total")),
		"base_net_total": flt(doc.get("base_net_total")),
	}

	net_total = sum(flt(item.get("net_amount")) for item in eligible_items)
	base_freight = flt(doc.get("base_custom_freight_and_forwarding_charges"))
	allocated = 0
	base_allocated = 0

	for index, item in enumerate(eligible_items):
		doc.flags.custom_freight_original_item_values.append(
			{
				"item": item,
				"rate": flt(item.get("rate")),
				"base_rate": flt(item.get("base_rate")),
				"amount": flt(item.get("amount")),
				"base_amount": flt(item.get("base_amount")),
				"net_amount": flt(item.get("net_amount")),
				"base_net_amount": flt(item.get("base_net_amount")),
				"net_rate": flt(item.get("net_rate")),
				"base_net_rate": flt(item.get("base_net_rate")),
			}
		)

		if index == len(eligible_items) - 1:
			item_freight = flt(freight - allocated, item.precision("net_amount"))
			base_item_freight = flt(base_freight - base_allocated, item.precision("base_net_amount"))
		else:
			share = flt(item.get("net_amount")) / net_total if net_total else 0
			item_freight = flt(freight * share, item.precision("net_amount"))
			base_item_freight = flt(base_freight * share, item.precision("base_net_amount"))
			allocated += item_freight
			base_allocated += base_item_freight

		item.rate = flt(
			flt(item.get("rate")) + (item_freight / flt(item.qty)),
			item.precision("rate"),
		)
		item.base_rate = flt(
			flt(item.get("base_rate")) + (base_item_freight / flt(item.qty)),
			item.precision("base_rate"),
		)
		item.amount = flt(flt(item.get("amount")) + item_freight, item.precision("amount"))
		item.base_amount = flt(
			flt(item.get("base_amount")) + base_item_freight,
			item.precision("base_amount"),
		)
		item.net_rate = item.rate
		item.base_net_rate = item.base_rate
		item.net_amount = item.amount
		item.base_net_amount = item.base_amount


def restore_item_amounts_after_freight_tax_calculation(doc):
	for row in doc.flags.get("custom_freight_original_item_values") or []:
		item = row["item"]
		item.rate = row["rate"]
		item.base_rate = row["base_rate"]
		item.amount = row["amount"]
		item.base_amount = row["base_amount"]
		item.net_amount = row["net_amount"]
		item.base_net_amount = row["base_net_amount"]
		item.net_rate = row["net_rate"]
		item.base_net_rate = row["base_net_rate"]


def clear_freight_calculation_flags(doc):
	for key in (
		"custom_freight_original_item_values",
		"custom_freight_original_totals",
		"in_custom_freight_calculation",
	):
		if key in doc.flags:
			doc.flags.pop(key)


def replace_net_totals_after_freight_tax_calculation(doc):
	original_totals = doc.flags.get("custom_freight_original_totals") or {}
	for fieldname, value in original_totals.items():
		doc.set(fieldname, value)

	last_tax_total = flt(doc.get("taxes")[-1].total) if doc.get("taxes") else flt(doc.get("net_total"))
	doc.grand_total = flt(last_tax_total, doc.precision("grand_total"))
	doc.total_taxes_and_charges = flt(
		doc.grand_total - flt(doc.get("net_total")),
		doc.precision("total_taxes_and_charges"),
	)
	doc.base_grand_total = flt(
		doc.grand_total * (flt(doc.get("conversion_rate")) or 1),
		doc.precision("base_grand_total"),
	)
	doc.base_total_taxes_and_charges = flt(
		doc.base_grand_total - flt(doc.get("base_net_total")),
		doc.precision("base_total_taxes_and_charges"),
	)

	if doc.meta.get_field("rounded_total") and hasattr(doc, "set_rounded_total"):
		doc.set_rounded_total()


def remove_freight_tax_rows(doc, migrate_existing=False):
	freight_amount = flt(doc.get("custom_freight_and_forwarding_charges"))
	remaining_taxes = []
	removed_indices = set()

	for tax in doc.get("taxes"):
		if is_freight_tax_row(doc, tax):
			if tax.get("idx"):
				removed_indices.add(cint(tax.idx))
			if migrate_existing and not freight_amount:
				freight_amount += flt(tax.get("tax_amount"))
			continue

		remaining_taxes.append(tax)

	if migrate_existing and freight_amount and not flt(doc.get("custom_freight_and_forwarding_charges")):
		doc.custom_freight_and_forwarding_charges = freight_amount

	doc.set("taxes", remaining_taxes)
	for tax in doc.get("taxes"):
		if (
			removed_indices
			and tax.get("charge_type") in ("On Previous Row Amount", "On Previous Row Total")
			and cint(tax.get("row_id")) in removed_indices
		):
			tax.charge_type = "On Net Total"
			tax.row_id = None
	renumber_taxes(doc)


def renumber_taxes(doc):
	for idx, tax in enumerate(doc.get("taxes"), start=1):
		tax.idx = idx


def is_freight_tax_row(doc, tax):
	if cint(tax.get("custom_is_freight_and_forwarding_row")):
		return True

	account = tax.get("account_head")
	if not account:
		return False

	configured_account = doc.get("custom_freight_account") or get_default_freight_account(doc.get("company"))
	if configured_account and account == configured_account and tax.get("charge_type") == "Actual":
		return True

	return False


def get_default_freight_account(company):
	if not company:
		return None

	account = frappe.db.get_value(
		"Account",
		{
			"company": company,
			"is_group": 0,
			"disabled": 0,
			"account_name": "Freight and Forwarding Charges",
		},
		"name",
	)
	if account:
		return account

	return frappe.db.get_value(
		"Account",
		{
			"company": company,
			"is_group": 0,
			"disabled": 0,
			"name": ("like", "%Freight%Forwarding%"),
		},
		"name",
	)


class SalesInvoiceFreightMixin:
	def make_tax_gl_entries(self, gl_entries):
		super().make_tax_gl_entries(gl_entries)
		self.make_custom_freight_gl_entry(gl_entries)

	def make_custom_freight_gl_entry(self, gl_entries):
		freight = flt(
			self.get("custom_freight_and_forwarding_charges"),
			self.precision("custom_freight_and_forwarding_charges"),
		)
		if not freight:
			return

		account = self.get("custom_freight_account")
		if not account:
			return

		base_freight = flt(
			self.get("base_custom_freight_and_forwarding_charges")
			or freight * (flt(self.get("conversion_rate")) or 1),
			self.precision("base_custom_freight_and_forwarding_charges"),
		)
		account_currency = get_account_currency(account)

		gl_entries.append(
			self.get_gl_dict(
				{
					"account": account,
					"against": self.customer,
					"credit": base_freight,
					"credit_in_account_currency": (
						base_freight if account_currency == self.company_currency else freight
					),
					"credit_in_transaction_currency": freight,
					"cost_center": self.cost_center,
				},
				account_currency,
				item=self,
			)
		)

import json

import frappe
from frappe import _
from frappe.utils import cint, flt, round_based_on_smallest_currency_fraction

from erpnext.accounts.utils import get_account_currency


SUPPORTED_DOCTYPES = ("Quotation", "Sales Order", "Sales Invoice")
FREIGHT_DESCRIPTION = "Freight & Forwarding"


def before_validate(doc, method=None):
	if doc.doctype not in SUPPORTED_DOCTYPES:
		return

	normalize_sales_freight(doc, recalculate=False)


def validate(doc, method=None):
	if doc.doctype not in SUPPORTED_DOCTYPES:
		return

	normalize_sales_freight(doc, recalculate=True)


def has_docfield(doc, fieldname):
	return bool(getattr(doc, "meta", None) and doc.meta.has_field(fieldname))


def precision(doc, fieldname):
	return doc.precision(fieldname) if has_docfield(doc, fieldname) else 2


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
		migrate_legacy_single_freight_to_child_table(doc)

		if recalculate:
			doc.calculate_taxes_and_totals()
			calculate_freight_rows_and_totals(doc)
			if flt(doc.get("custom_total_freight_and_forwarding")):
				apply_freight_to_document_totals(doc)
			else:
				set_post_calculation_fields(doc)
		else:
			calculate_freight_rows_and_totals(doc)

		validate_freight(doc)
	finally:
		doc.flags.in_custom_freight_calculation = False


def migrate_legacy_single_freight_to_child_table(doc):
	if doc.get("custom_freight_charges"):
		return

	legacy_amount = flt(doc.get("custom_freight_and_forwarding_charges"))
	if not legacy_amount:
		return

	doc.append(
		"custom_freight_charges",
		{
			"charge_type": "Actual",
			"account_head": doc.get("custom_freight_account") or get_default_freight_account(doc.company),
			"description": FREIGHT_DESCRIPTION,
			"amount": legacy_amount,
			"taxable": cint(doc.get("custom_freight_taxable", 1)),
			"cost_center": doc.get("cost_center"),
		},
	)


def calculate_freight_rows_and_totals(doc):
	total_freight = 0
	taxable_freight = 0
	base_total_freight = 0
	base_taxable_freight = 0
	conversion_rate = flt(doc.get("conversion_rate")) or 1

	for row in doc.get("custom_freight_charges") or []:
		if not row.get("charge_type"):
			row.charge_type = "Actual"

		if not row.get("description"):
			row.description = row.get("account_head") or FREIGHT_DESCRIPTION

		if row.charge_type == "Percentage":
			row.amount = flt(
				flt(doc.get("net_total")) * flt(row.get("rate")) / 100,
				row.precision("amount"),
			)
		else:
			row.rate = 0
			row.amount = flt(row.get("amount"), row.precision("amount"))

		row.base_amount = flt(row.amount * conversion_rate, row.precision("base_amount"))

		total_freight += row.amount
		base_total_freight += row.base_amount
		if cint(row.get("taxable")):
			taxable_freight += row.amount
			base_taxable_freight += row.base_amount

	doc.custom_total_freight_and_forwarding = flt(
		total_freight, doc.precision("custom_total_freight_and_forwarding")
	)
	doc.base_custom_total_freight_and_forwarding = flt(
		base_total_freight, doc.precision("base_custom_total_freight_and_forwarding")
	)
	doc.custom_taxable_freight_total = flt(
		taxable_freight, doc.precision("custom_taxable_freight_total")
	)
	doc.base_custom_taxable_freight_total = flt(
		base_taxable_freight, doc.precision("base_custom_taxable_freight_total")
	)
	doc.custom_taxable_value_with_freight = flt(
		flt(doc.get("net_total")) + taxable_freight,
		doc.precision("custom_taxable_value_with_freight"),
	)
	doc.base_custom_taxable_value_with_freight = flt(
		flt(doc.get("base_net_total")) + base_taxable_freight,
		doc.precision("base_custom_taxable_value_with_freight"),
	)


def apply_freight_to_document_totals(doc):
	taxable_freight = flt(doc.get("custom_taxable_freight_total"))
	total_freight = flt(doc.get("custom_total_freight_and_forwarding"))

	if taxable_freight:
		allocate_freight_to_items_for_tax_calculation(doc, taxable_freight)
		doc.calculate_taxes_and_totals()
		restore_item_amounts_after_freight_tax_calculation(doc)
		set_item_taxable_values_from_freight_allocation(doc)
	else:
		doc.calculate_taxes_and_totals()
		clear_item_freight_taxable_values(doc)

	replace_totals_after_freight_tax_calculation(doc, total_freight, taxable_freight)
	set_post_calculation_fields(doc)


def allocate_freight_to_items_for_tax_calculation(doc, taxable_freight):
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
	doc.flags.custom_freight_item_allocations = []

	net_total = sum(flt(item.get("net_amount")) for item in eligible_items)
	base_taxable_freight = flt(doc.get("base_custom_taxable_freight_total"))
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
				"taxable_value": flt(item.get("taxable_value")),
				"additional_taxable_value": flt(item.get("additional_taxable_value")),
			}
		)

		if index == len(eligible_items) - 1:
			item_freight = flt(taxable_freight - allocated, item.precision("net_amount"))
			base_item_freight = flt(
				base_taxable_freight - base_allocated, item.precision("base_net_amount")
			)
		else:
			share = flt(item.get("net_amount")) / net_total if net_total else 0
			item_freight = flt(taxable_freight * share, item.precision("net_amount"))
			base_item_freight = flt(base_taxable_freight * share, item.precision("base_net_amount"))
			allocated += item_freight
			base_allocated += base_item_freight

		doc.flags.custom_freight_item_allocations.append(
			{"item": item, "amount": item_freight, "base_amount": base_item_freight}
		)

		# Temporary calculation-only values. Restored immediately after ERPNext computes tax rows.
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


def set_item_taxable_values_from_freight_allocation(doc):
	allocations = {row["item"].name or row["item"].idx: row for row in doc.flags.get("custom_freight_item_allocations") or []}
	for item in doc.get("items"):
		has_additional_taxable_value = has_docfield(item, "additional_taxable_value")
		has_taxable_value = has_docfield(item, "taxable_value")
		key = item.name or item.idx
		allocation = allocations.get(key)
		if not allocation:
			if has_additional_taxable_value:
				item.additional_taxable_value = 0
			if has_taxable_value:
				item.taxable_value = flt(item.get("amount"), precision(item, "taxable_value"))
			continue

		if has_additional_taxable_value:
			item.additional_taxable_value = flt(
				allocation["amount"], precision(item, "additional_taxable_value")
			)
		if has_taxable_value:
			item.taxable_value = flt(
				flt(item.get("amount")) + allocation["amount"], precision(item, "taxable_value")
			)


def clear_item_freight_taxable_values(doc):
	for item in doc.get("items"):
		if has_docfield(item, "additional_taxable_value"):
			item.additional_taxable_value = 0
		if has_docfield(item, "taxable_value"):
			item.taxable_value = flt(item.get("amount"), precision(item, "taxable_value"))


def replace_totals_after_freight_tax_calculation(doc, total_freight, taxable_freight):
	original_totals = doc.flags.get("custom_freight_original_totals") or {
		"total": flt(doc.get("total")),
		"base_total": flt(doc.get("base_total")),
		"net_total": flt(doc.get("net_total")),
		"base_net_total": flt(doc.get("base_net_total")),
	}
	for fieldname, value in original_totals.items():
		doc.set(fieldname, value)

	tax_total = get_tax_total_excluding_freight(doc, taxable_freight)
	base_tax_total = flt(tax_total * (flt(doc.get("conversion_rate")) or 1))

	doc.grand_total = flt(
		flt(doc.get("net_total")) + total_freight + tax_total,
		doc.precision("grand_total"),
	)
	doc.base_grand_total = flt(
		flt(doc.get("base_net_total")) + flt(doc.get("base_custom_total_freight_and_forwarding")) + base_tax_total,
		doc.precision("base_grand_total"),
	)
	doc.total_taxes_and_charges = flt(
		total_freight + tax_total, doc.precision("total_taxes_and_charges")
	)
	doc.base_total_taxes_and_charges = flt(
		flt(doc.get("base_custom_total_freight_and_forwarding")) + base_tax_total,
		doc.precision("base_total_taxes_and_charges"),
	)

	set_rounded_totals_after_freight(doc)


def get_tax_total_excluding_freight(doc, taxable_freight):
	if not doc.get("taxes"):
		return 0

	if taxable_freight:
		# Last visible tax row total includes net total + taxable freight + taxes.
		return flt(doc.taxes[-1].total) - flt(doc.get("net_total")) - taxable_freight

	return flt(doc.get("total_taxes_and_charges"))


def set_rounded_totals_after_freight(doc):
	if not doc.meta.get_field("rounded_total"):
		return

	if hasattr(doc, "is_rounded_total_disabled") and doc.is_rounded_total_disabled():
		doc.rounded_total = 0
		doc.rounding_adjustment = 0
	else:
		doc.rounded_total = round_based_on_smallest_currency_fraction(
			doc.grand_total, doc.currency, doc.precision("rounded_total")
		)
		doc.rounding_adjustment = flt(
			doc.rounded_total - doc.grand_total, doc.precision("rounding_adjustment")
		)

	if doc.meta.get_field("base_rounded_total"):
		doc.base_rounded_total = flt(
			doc.rounded_total * (flt(doc.get("conversion_rate")) or 1),
			doc.precision("base_rounded_total"),
		)
	if doc.meta.get_field("base_rounding_adjustment"):
		doc.base_rounding_adjustment = flt(
			doc.rounding_adjustment * (flt(doc.get("conversion_rate")) or 1),
			doc.precision("base_rounding_adjustment"),
		)


def set_post_calculation_fields(doc):
	calculate_freight_rows_and_totals(doc)
	set_rounded_totals_after_freight(doc)
	update_payment_schedule(doc)
	if hasattr(doc, "set_total_in_words"):
		doc.set_total_in_words()
	if doc.doctype == "Sales Invoice" and hasattr(doc, "calculate_outstanding_amount"):
		doc.calculate_outstanding_amount()
	clear_freight_calculation_flags(doc)


def update_payment_schedule(doc):
	if not doc.meta.get_field("payment_schedule"):
		return

	if doc.get("payment_schedule"):
		for row in doc.get("payment_schedule"):
			if not flt(row.get("invoice_portion")):
				row.invoice_portion = 100 if len(doc.payment_schedule) == 1 else 0

	doc.set_payment_schedule()


def clear_freight_calculation_flags(doc):
	for key in (
		"custom_freight_original_item_values",
		"custom_freight_original_totals",
		"custom_freight_item_allocations",
		"in_custom_freight_calculation",
	):
		if key in doc.flags:
			doc.flags.pop(key)


def validate_freight(doc):
	for row in doc.get("custom_freight_charges") or []:
		if flt(row.get("amount")) < 0 and not cint(doc.get("is_return")):
			frappe.throw(_("Row {0}: Freight amount cannot be negative.").format(row.idx))
		if flt(row.get("amount")) and not row.get("account_head"):
			frappe.throw(_("Row {0}: Please select Account Head for Freight.").format(row.idx))


def remove_freight_tax_rows(doc, migrate_existing=False):
	remaining_taxes = []
	removed_indices = set()
	migrated_amount = 0
	migrated_account = None
	migrated_taxable = 1

	for tax in doc.get("taxes"):
		if is_freight_tax_row(doc, tax):
			if tax.get("idx"):
				removed_indices.add(cint(tax.idx))
			if migrate_existing:
				migrated_amount += flt(tax.get("tax_amount"))
				migrated_account = migrated_account or tax.get("account_head")
			continue

		remaining_taxes.append(tax)

	if migrate_existing and migrated_amount and not doc.get("custom_freight_charges"):
		doc.append(
			"custom_freight_charges",
			{
				"charge_type": "Actual",
				"account_head": migrated_account or get_default_freight_account(doc.get("company")),
				"description": FREIGHT_DESCRIPTION,
				"amount": migrated_amount,
				"taxable": migrated_taxable,
				"cost_center": doc.get("cost_center"),
			},
		)

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
	account = tax.get("account_head")
	if not account or tax.get("charge_type") != "Actual":
		return False

	freight_accounts = {row.account_head for row in doc.get("custom_freight_charges") or [] if row.account_head}
	legacy_account = doc.get("custom_freight_account") or get_default_freight_account(doc.get("company"))
	if legacy_account:
		freight_accounts.add(legacy_account)

	return account in freight_accounts


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
		self.make_custom_freight_gl_entries(gl_entries)

	def make_custom_freight_gl_entries(self, gl_entries):
		for row in self.get("custom_freight_charges") or []:
			amount = flt(row.get("amount"), row.precision("amount"))
			if not amount or not row.get("account_head"):
				continue

			base_amount = flt(
				row.get("base_amount") or amount * (flt(self.get("conversion_rate")) or 1),
				row.precision("base_amount"),
			)
			account_currency = get_account_currency(row.account_head)
			cost_center = row.get("cost_center") or self.get("cost_center")

			gl_entries.append(
				self.get_gl_dict(
					{
						"account": row.account_head,
						"against": self.customer,
						"credit": base_amount,
						"credit_in_account_currency": (
							base_amount if account_currency == self.company_currency else amount
						),
						"credit_in_transaction_currency": amount,
						"cost_center": cost_center,
						"project": self.get("project"),
					},
					account_currency,
					item=row,
				)
			)

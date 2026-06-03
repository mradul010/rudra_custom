import json

import frappe
from frappe.utils import flt, getdate, rounded

DEBUG_TITLE = "RUDRA TCS DEBUG"


# ─── before_validate hook ────────────────────────────────────────────────────

def fix_tcs_on_excess_amount(doc, method=None):
	"""before_validate hook on Purchase Invoice.

	Computes the correct TDS/TCS withholding amount from tabPurchase Invoice
	(bypassing the incomplete tabTax Withholding Entry on this site), then:
	  - sets doc.override_tax_withholding_entries = 1
	  - pre-populates doc.tax_withholding_entries with correct TWE rows

	The native TaxWithholdingController.on_validate() then:
	  - skips _generate_withholding_entries()  (because override = 1)
	  - runs _process_withholding_entries() using our correct TWE rows
	  - updates the tax row, item_wise_tax_detail, and grand_total correctly

	We do NOT touch tax rows, item_wise_tax_detail, or grand_total ourselves.
	"""
	if not doc.get("apply_tds"):
		return

	details = _get_calculation_details(doc)
	if details.get("return_reason"):
		_log("Early return", {"reason": details["return_reason"], **_safe(details)})
		return

	category_name = details["category"]
	rate = details["rate"]
	current_amount = details["current_taxable_amount"]
	previous_amount = details["previous_submitted_taxable_amount"]
	threshold = details["threshold"]
	unused_threshold = details["unused_threshold"]
	taxable_excess = details["taxable_excess"]
	withholding_amount = details["withholding_amount"]

	_log("Calculation complete", {
		"invoice": doc.get("name"),
		"supplier": doc.get("supplier"),
		"category": category_name,
		"tax_deduction_basis": details.get("tax_deduction_basis"),
		"current_taxable_amount": current_amount,
		"previous_submitted": previous_amount,
		"threshold": threshold,
		"unused_threshold": unused_threshold,
		"taxable_excess": taxable_excess,
		"rate": rate,
		"withholding_amount": withholding_amount,
	})

	# Tell native TaxWithholdingController to use our TWE rows instead of
	# regenerating from the incomplete tabTax Withholding Entry.
	doc.override_tax_withholding_entries = 1
	_log("override_tax_withholding_entries set", {"value": 1})

	# Clear existing TWE rows for this category; preserve any others.
	rows_to_remove = [
		r for r in doc.get("tax_withholding_entries")
		if r.get("tax_withholding_category") == category_name
	]
	for row in rows_to_remove:
		doc.remove(row)

	# Base fields shared by all TWE rows for this invoice/category.
	base_twe = {
		"company": doc.company,
		"party_type": "Supplier",
		"party": doc.supplier,
		"tax_withholding_category": category_name,
		"tax_rate": rate,
		"taxable_doctype": doc.doctype,
		"taxable_name": doc.name,
		"taxable_date": doc.posting_date,
		"withholding_doctype": doc.doctype,
		"withholding_name": doc.name,
		"withholding_date": doc.posting_date,
		"conversion_rate": doc.get("conversion_rate") or 1,
		"status": "Settled",
	}

	# Threshold exemption row — documents the portion covered by the threshold.
	if unused_threshold > 0 and current_amount > 0:
		exempt_amount = min(unused_threshold, current_amount)
		doc.append("tax_withholding_entries", {
			**base_twe,
			"taxable_amount": exempt_amount,
			"withholding_amount": 0,
			"under_withheld_reason": "Threshold Exemption",
		})
		_log("Appended threshold exemption TWE row", {"exempt_amount": exempt_amount})

	# Main withholding row — only when there is taxable excess.
	if withholding_amount > 0:
		doc.append("tax_withholding_entries", {
			**base_twe,
			"taxable_amount": taxable_excess,
			"withholding_amount": withholding_amount,
		})
		_log("Appended main TWE row", {
			"taxable_excess": taxable_excess,
			"withholding_amount": withholding_amount,
		})


# ─── Core calculation ────────────────────────────────────────────────────────

def _get_calculation_details(doc):
	"""Compute all TCS/TDS details. Check 'return_reason' for early-exit."""
	category_name = _get_category_name(doc)
	if not category_name:
		return {"return_reason": "No tax_withholding_category on supplier or invoice"}

	if not frappe.db.exists("Tax Withholding Category", category_name):
		return {"return_reason": f"Tax Withholding Category does not exist: {category_name}"}

	category = frappe.get_cached_doc("Tax Withholding Category", category_name)

	if not category.get("tax_on_excess_amount"):
		return {"category": category_name, "return_reason": "tax_on_excess_amount is disabled"}

	if category.get("disable_cumulative_threshold"):
		return {"category": category_name, "return_reason": "disable_cumulative_threshold is enabled"}

	rate_row = _get_rate_row(category, doc.posting_date)
	if not rate_row:
		return {
			"category": category_name,
			"return_reason": f"No applicable rate row for posting_date {doc.get('posting_date')}",
		}

	threshold = flt(rate_row.get("cumulative_threshold"))
	rate = flt(rate_row.get("tax_withholding_rate"))

	if not threshold:
		return {"category": category_name, "return_reason": "Cumulative threshold is zero"}
	if not rate:
		return {"category": category_name, "return_reason": "Tax withholding rate is zero"}

	account = _get_account(category, doc.company)
	if not account:
		return {
			"category": category_name,
			"return_reason": f"No account configured for company: {doc.get('company')}",
		}

	# Freeze current and previous amounts before any doc modification.
	current_amount = _get_current_amount(doc, category, account)
	previous_amount = _get_previous_amount(doc, category, rate_row)

	unused_threshold = max(threshold - previous_amount, 0)
	taxable_excess = max(current_amount - unused_threshold, 0)

	raw = taxable_excess * rate / 100
	withholding_amount = flt(rounded(raw)) if category.get("round_off_tax_amount") else flt(raw, 2)

	return {
		"category": category_name,
		"tax_deduction_basis": category.get("tax_deduction_basis"),
		"previous_submitted_taxable_amount": previous_amount,
		"current_taxable_amount": current_amount,
		"threshold": threshold,
		"unused_threshold": unused_threshold,
		"taxable_excess": taxable_excess,
		"rate": rate,
		"withholding_amount": withholding_amount,
		"account": account,
		"rate_row": rate_row,
		"return_reason": None,
	}


# ─── Calculation helpers ─────────────────────────────────────────────────────

def _get_category_name(doc):
	if doc.get("tax_withholding_category"):
		return doc.get("tax_withholding_category")
	return frappe.db.get_value("Supplier", doc.get("supplier"), "tax_withholding_category")


def _get_rate_row(category, posting_date):
	posting_date = getdate(posting_date)
	for row in category.get("rates"):
		if getdate(row.get("from_date")) <= posting_date <= getdate(row.get("to_date")):
			return row
	return None


def _is_gross_total_basis(category):
	"""True for TCS (Gross Total / Grand Total), False for TDS (Net Total)."""
	if category.get("tax_deduction_basis") in {"Gross Total", "Grand Total"}:
		return True
	# Name-based fallback for categories where field may still be misconfigured.
	n = (category.get("name") or "").upper()
	return "TCS" in n or "COLLECTED" in n


def _get_current_amount(doc, category, account=None):
	"""Taxable amount for THIS invoice, frozen before any hook modification.

	For Gross Total basis: adds back any existing TCS deduct row so the
	calculation is always on the original pre-deduction invoice value, even
	when the hook is re-invoked on a previously-processed invoice.
	"""
	if _is_gross_total_basis(category):
		existing = _find_tax_row(doc, account) if account else None
		if existing and existing.get("add_deduct_tax") == "Deduct":
			# grand_total has been reduced by TCS in a previous save;
			# use grand_total + TCS (not rounded_total) to avoid precision loss.
			return flt(doc.get("grand_total")) + flt(existing.get("tax_amount", 0))
		return flt(doc.get("rounded_total") or doc.get("grand_total"))
	return flt(doc.get("net_total"))


def _find_tax_row(doc, account):
	for row in doc.get("taxes"):
		if row.get("account_head") == account and row.get("is_tax_withholding_account"):
			return row
	return None


def _get_previous_amount(doc, category, rate_row):
	"""Sum taxable amounts from prior submitted Purchase Invoices in the FY.

	Never reads tabTax Withholding Entry — that table is incomplete on this site.

	For Gross Total basis: adds back withholding-deduct rows from each prior
	invoice to restore the original pre-TCS value stored in the DB.
	"""
	if _is_gross_total_basis(category):
		sql = """
		SELECT COALESCE(SUM(
			pi.grand_total
			+ COALESCE((
				SELECT SUM(pt.tax_amount)
				FROM `tabPurchase Taxes and Charges` pt
				WHERE pt.parent = pi.name
				  AND pt.is_tax_withholding_account = 1
				  AND pt.add_deduct_tax = 'Deduct'
			), 0)
		), 0)
		FROM `tabPurchase Invoice` pi
		WHERE pi.supplier = %(supplier)s
		  AND pi.company = %(company)s
		  AND pi.posting_date BETWEEN %(from_date)s AND %(to_date)s
		  AND pi.name != %(name)s
		  AND pi.docstatus = 1
		  AND pi.apply_tds = 1
		"""
	else:
		sql = """
		SELECT COALESCE(SUM(net_total), 0)
		FROM `tabPurchase Invoice`
		WHERE supplier = %(supplier)s
		  AND company = %(company)s
		  AND posting_date BETWEEN %(from_date)s AND %(to_date)s
		  AND name != %(name)s
		  AND docstatus = 1
		  AND apply_tds = 1
		"""

	result = frappe.db.sql(sql, {
		"supplier": doc.supplier,
		"company": doc.company,
		"from_date": rate_row.from_date,
		"to_date": rate_row.to_date,
		"name": doc.name or "",
	})
	return flt(result[0][0]) if result else 0.0


def _get_account(category, company):
	for row in category.get("accounts"):
		if row.get("company") == company:
			return row.get("account")
	return None


# ─── Debug whitelist API ─────────────────────────────────────────────────────

@frappe.whitelist()
def debug_tcs_for_invoice(invoice_name):
	"""Read-only: inspect TCS/TDS calculation for an invoice without modifying it."""
	doc = frappe.get_doc("Purchase Invoice", invoice_name)
	details = _get_calculation_details(doc)

	return {
		"invoice": invoice_name,
		"supplier": doc.supplier,
		"company": doc.company,
		"category": details.get("category"),
		"tax_deduction_basis": details.get("tax_deduction_basis"),
		"previous_source": "Purchase Invoice",
		"previous_submitted_taxable_amount": details.get("previous_submitted_taxable_amount"),
		"current_taxable_amount": details.get("current_taxable_amount"),
		"threshold": details.get("threshold"),
		"unused_threshold": details.get("unused_threshold"),
		"taxable_excess": details.get("taxable_excess"),
		"rate": details.get("rate"),
		"withholding_amount": details.get("withholding_amount"),
		"account": details.get("account"),
		"return_reason": details.get("return_reason"),
	}


# ─── Utilities ───────────────────────────────────────────────────────────────

def _safe(data):
	return {k: v for k, v in data.items() if k != "rate_row" and not hasattr(v, "doctype")}


def _log(stage, data):
	frappe.logger(DEBUG_TITLE).info(
		json.dumps({"stage": stage, **_safe(data)}, default=str, indent=2)
	)

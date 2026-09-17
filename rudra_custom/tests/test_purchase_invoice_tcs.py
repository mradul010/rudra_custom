from contextlib import contextmanager
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe

from rudra_custom.overrides import purchase_invoice_tcs


def _obj(**kwargs):
	return frappe._dict(kwargs)


@contextmanager
def _patched_sql(rows):
	queries = []

	def fake_sql(sql, params):
		queries.append(sql)
		from_date = str(params["from_date"])
		to_date = str(params["to_date"])

		total = 0
		for row in rows:
			if row.supplier != params["supplier"]:
				continue
			if row.company != params["company"]:
				continue
			if not (from_date <= row.posting_date <= to_date):
				continue
			if row.name == params["name"]:
				continue
			if row.docstatus != 1:
				continue

			if "grand_total" in sql:
				total += row.grand_total + row.withholding_tax
			else:
				total += row.net_total

		return [(total,)]

	fake_frappe = SimpleNamespace(db=SimpleNamespace(sql=fake_sql))
	with patch.object(purchase_invoice_tcs, "frappe", fake_frappe):
		yield queries


class TestPreviousPurchaseThresholdAggregation(TestCase):
	def setUp(self):
		self.doc = SimpleNamespace(
			name="CURRENT-PI",
			supplier="Supplier A",
			company="Company A",
		)
		self.rate_row = SimpleNamespace(from_date="2026-04-01", to_date="2027-03-31")
		self.gross_category = _obj(tax_deduction_basis="Gross Total", name="TDS - Test")
		self.net_category = _obj(tax_deduction_basis="Net Total", name="TDS - Test")

	def get_previous_amount(self, rows, category=None):
		with _patched_sql(rows) as queries:
			amount = purchase_invoice_tcs._get_previous_amount(
				self.doc,
				category or self.gross_category,
				self.rate_row,
			)

		self.assertNotIn("apply_tds", queries[0])
		return amount

	def test_previous_invoice_without_tds_counts_toward_threshold(self):
		rows = [
			_obj(
				name="PI-1",
				supplier="Supplier A",
				company="Company A",
				posting_date="2026-05-01",
				docstatus=1,
				apply_tds=0,
				net_total=1000000,
				grand_total=1000000,
				withholding_tax=0,
			),
		]

		self.assertEqual(self.get_previous_amount(rows), 1000000)

	def test_multiple_historical_invoices_without_tds_count(self):
		rows = [
			_obj(
				name=f"PI-{idx}",
				supplier="Supplier A",
				company="Company A",
				posting_date=f"2026-05-0{idx}",
				docstatus=1,
				apply_tds=0,
				net_total=amount,
				grand_total=amount,
				withholding_tax=0,
			)
			for idx, amount in enumerate([1000000, 1500000, 2000000], start=1)
		]

		self.assertEqual(self.get_previous_amount(rows), 4500000)

	def test_below_threshold_previous_amount_is_still_reported(self):
		rows = [
			_obj(
				name="PI-1",
				supplier="Supplier A",
				company="Company A",
				posting_date="2026-05-01",
				docstatus=1,
				apply_tds=0,
				net_total=500000,
				grand_total=500000,
				withholding_tax=0,
			),
		]

		self.assertEqual(self.get_previous_amount(rows), 500000)

	def test_threshold_crossed_on_current_invoice_uses_prior_amount(self):
		rows = [
			_obj(
				name="PI-1",
				supplier="Supplier A",
				company="Company A",
				posting_date="2026-05-01",
				docstatus=1,
				apply_tds=0,
				net_total=4900000,
				grand_total=4900000,
				withholding_tax=0,
			),
		]

		previous = self.get_previous_amount(rows)
		unused_threshold = max(5000000 - previous, 0)

		self.assertEqual(unused_threshold, 100000)
		self.assertEqual(max(500000 - unused_threshold, 0), 400000)

	def test_after_threshold_already_crossed_current_amount_is_fully_eligible(self):
		rows = [
			_obj(
				name="PI-1",
				supplier="Supplier A",
				company="Company A",
				posting_date="2026-05-01",
				docstatus=1,
				apply_tds=0,
				net_total=5100000,
				grand_total=5100000,
				withholding_tax=0,
			),
		]

		previous = self.get_previous_amount(rows)
		unused_threshold = max(5000000 - previous, 0)

		self.assertEqual(unused_threshold, 0)
		self.assertEqual(max(1000000 - unused_threshold, 0), 1000000)

	def test_purchase_return_reduces_cumulative_value(self):
		rows = [
			_obj(
				name="PI-1",
				supplier="Supplier A",
				company="Company A",
				posting_date="2026-05-01",
				docstatus=1,
				apply_tds=0,
				net_total=2000000,
				grand_total=2000000,
				withholding_tax=0,
			),
			_obj(
				name="PI-RETURN",
				supplier="Supplier A",
				company="Company A",
				posting_date="2026-05-15",
				docstatus=1,
				apply_tds=0,
				net_total=-200000,
				grand_total=-200000,
				withholding_tax=0,
			),
		]

		self.assertEqual(self.get_previous_amount(rows), 1800000)

	def test_cancelled_invoice_does_not_contribute(self):
		rows = [
			_obj(
				name="PI-CANCELLED",
				supplier="Supplier A",
				company="Company A",
				posting_date="2026-05-01",
				docstatus=2,
				apply_tds=0,
				net_total=1000000,
				grand_total=1000000,
				withholding_tax=0,
			),
		]

		self.assertEqual(self.get_previous_amount(rows), 0)

	def test_amended_invoice_counts_replacement_once(self):
		rows = [
			_obj(
				name="PI-1",
				supplier="Supplier A",
				company="Company A",
				posting_date="2026-05-01",
				docstatus=2,
				apply_tds=0,
				net_total=1000000,
				grand_total=1000000,
				withholding_tax=0,
			),
			_obj(
				name="PI-1-1",
				supplier="Supplier A",
				company="Company A",
				posting_date="2026-05-01",
				docstatus=1,
				apply_tds=0,
				net_total=1200000,
				grand_total=1200000,
				withholding_tax=0,
			),
		]

		self.assertEqual(self.get_previous_amount(rows), 1200000)

	def test_different_supplier_does_not_contribute(self):
		rows = [
			_obj(
				name="PI-OTHER",
				supplier="Supplier B",
				company="Company A",
				posting_date="2026-05-01",
				docstatus=1,
				apply_tds=0,
				net_total=1000000,
				grand_total=1000000,
				withholding_tax=0,
			),
		]

		self.assertEqual(self.get_previous_amount(rows), 0)

	def test_different_company_does_not_contribute(self):
		rows = [
			_obj(
				name="PI-OTHER",
				supplier="Supplier A",
				company="Company B",
				posting_date="2026-05-01",
				docstatus=1,
				apply_tds=0,
				net_total=1000000,
				grand_total=1000000,
				withholding_tax=0,
			),
		]

		self.assertEqual(self.get_previous_amount(rows), 0)

	def test_net_basis_query_also_ignores_historical_apply_tds(self):
		rows = [
			_obj(
				name="PI-1",
				supplier="Supplier A",
				company="Company A",
				posting_date="2026-05-01",
				docstatus=1,
				apply_tds=0,
				net_total=750000,
				grand_total=885000,
				withholding_tax=0,
			),
		]

		self.assertEqual(self.get_previous_amount(rows, self.net_category), 750000)

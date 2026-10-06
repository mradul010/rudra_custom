import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


SALES_DOCTYPES = ("Quotation", "Sales Order", "Sales Invoice")


def execute():
	custom_fields = {}
	for doctype in SALES_DOCTYPES:
		custom_fields[doctype] = get_sales_freight_child_table_fields()

	create_custom_fields(custom_fields, ignore_validate=True, update=True)
	hide_legacy_single_freight_fields()


def get_sales_freight_child_table_fields():
	return [
		{
			"fieldname": "custom_freight_section",
			"label": "Freight & Forwarding",
			"fieldtype": "Section Break",
			"insert_after": "net_total",
			"collapsible": 0,
		},
		{
			"fieldname": "custom_freight_charges",
			"label": "Freight & Forwarding Charges",
			"fieldtype": "Table",
			"options": "Freight and Forwarding Charge",
			"insert_after": "custom_freight_section",
		},
		{
			"fieldname": "custom_total_freight_and_forwarding",
			"label": "Total Freight & Forwarding",
			"fieldtype": "Currency",
			"options": "currency",
			"read_only": 1,
			"insert_after": "custom_freight_charges",
		},
		{
			"fieldname": "base_custom_total_freight_and_forwarding",
			"label": "Base Total Freight & Forwarding",
			"fieldtype": "Currency",
			"options": "Company:company:default_currency",
			"read_only": 1,
			"insert_after": "custom_total_freight_and_forwarding",
		},
		{
			"fieldname": "custom_taxable_freight_total",
			"label": "Taxable Freight Total",
			"fieldtype": "Currency",
			"options": "currency",
			"read_only": 1,
			"insert_after": "base_custom_total_freight_and_forwarding",
		},
		{
			"fieldname": "base_custom_taxable_freight_total",
			"label": "Base Taxable Freight Total",
			"fieldtype": "Currency",
			"options": "Company:company:default_currency",
			"read_only": 1,
			"insert_after": "custom_taxable_freight_total",
		},
		{
			"fieldname": "custom_taxable_value_with_freight",
			"label": "Taxable Value With Freight",
			"fieldtype": "Currency",
			"options": "currency",
			"read_only": 1,
			"insert_after": "base_custom_taxable_freight_total",
		},
		{
			"fieldname": "base_custom_taxable_value_with_freight",
			"label": "Base Taxable Value With Freight",
			"fieldtype": "Currency",
			"options": "Company:company:default_currency",
			"read_only": 1,
			"insert_after": "custom_taxable_value_with_freight",
		},
	]


def hide_legacy_single_freight_fields():
	for doctype in SALES_DOCTYPES:
		for fieldname in (
			"custom_freight_and_forwarding_charges",
			"base_custom_freight_and_forwarding_charges",
			"custom_freight_taxable",
			"custom_freight_column_break",
			"custom_freight_account",
		):
			custom_field = frappe.db.exists("Custom Field", f"{doctype}-{fieldname}")
			if custom_field:
				frappe.db.set_value(
					"Custom Field",
					custom_field,
					{
						"hidden": 1,
						"read_only": 1,
						"print_hide": 1,
					},
				)

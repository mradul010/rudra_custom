from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
	custom_fields = {}
	for doctype in ("Quotation", "Sales Order", "Sales Invoice"):
		custom_fields[doctype] = get_sales_freight_fields()

	create_custom_fields(custom_fields, ignore_validate=True, update=True)


def get_sales_freight_fields():
	return [
		{
			"fieldname": "custom_freight_section",
			"label": "Freight & Forwarding",
			"fieldtype": "Section Break",
			"insert_after": "net_total",
			"collapsible": 1,
		},
		{
			"fieldname": "custom_freight_and_forwarding_charges",
			"label": "Freight & Forwarding Charges",
			"fieldtype": "Currency",
			"options": "currency",
			"default": "0",
			"insert_after": "custom_freight_section",
		},
		{
			"fieldname": "base_custom_freight_and_forwarding_charges",
			"label": "Base Freight & Forwarding Charges",
			"fieldtype": "Currency",
			"options": "Company:company:default_currency",
			"read_only": 1,
			"insert_after": "custom_freight_and_forwarding_charges",
		},
		{
			"fieldname": "custom_freight_taxable",
			"label": "Freight Taxable",
			"fieldtype": "Check",
			"default": "1",
			"insert_after": "base_custom_freight_and_forwarding_charges",
		},
		{
			"fieldname": "custom_freight_column_break",
			"fieldtype": "Column Break",
			"insert_after": "custom_freight_taxable",
		},
		{
			"fieldname": "custom_freight_account",
			"label": "Freight & Forwarding Account",
			"fieldtype": "Link",
			"options": "Account",
			"insert_after": "custom_freight_column_break",
		},
		{
			"fieldname": "custom_taxable_value_with_freight",
			"label": "Taxable Value With Freight",
			"fieldtype": "Currency",
			"options": "currency",
			"read_only": 1,
			"insert_after": "custom_freight_account",
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

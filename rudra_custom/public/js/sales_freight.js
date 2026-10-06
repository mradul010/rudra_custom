(() => {
	const doctypes = ["Quotation", "Sales Order", "Sales Invoice"];

	function setup_freight_handlers(doctype) {
		frappe.ui.form.on(doctype, {
			setup(frm) {
				frm.set_query("custom_freight_account", () => ({
					filters: {
						company: frm.doc.company,
						is_group: 0,
						disabled: 0,
					},
				}));
			},

			custom_freight_and_forwarding_charges(frm) {
				recalculate_freight(frm);
			},

			custom_freight_taxable(frm) {
				recalculate_freight(frm);
			},

			custom_freight_account(frm) {
				recalculate_freight(frm);
			},

			company(frm) {
				frm.set_value("custom_freight_account", "");
			},
		});
	}

	function recalculate_freight(frm) {
		if (frm.__custom_freight_recalculating || frm.is_new() === undefined) return;

		frm.__custom_freight_recalculating = true;
		frappe.call({
			method: "rudra_custom.selling.sales_freight.recalculate_sales_freight",
			args: {
				doc: frm.doc,
			},
			freeze: false,
			callback(r) {
				if (!r.message) return;

				frappe.model.sync(r.message);
				frm.refresh_fields();
			},
			always() {
				frm.__custom_freight_recalculating = false;
			},
		});
	}

	doctypes.forEach(setup_freight_handlers);
})();

(() => {
	const doctypes = ["Quotation", "Sales Order", "Sales Invoice"];

	function setup_freight_handlers(doctype) {
		frappe.ui.form.on(doctype, {
			setup(frm) {
				frm.set_query("account_head", "custom_freight_charges", () => ({
					filters: {
						company: frm.doc.company,
						is_group: 0,
						disabled: 0,
					},
				}));
				frm.set_query("cost_center", "custom_freight_charges", () => ({
					filters: {
						company: frm.doc.company,
					},
				}));
			},

			company(frm) {
				(frm.doc.custom_freight_charges || []).forEach((row) => {
					row.account_head = "";
					row.cost_center = "";
				});
				frm.refresh_field("custom_freight_charges");
				recalculate_freight(frm);
			},

			custom_freight_charges_add(frm) {
				recalculate_freight(frm);
			},

			custom_freight_charges_remove(frm) {
				recalculate_freight(frm);
			},
		});
	}

	frappe.ui.form.on("Freight and Forwarding Charge", {
		charge_type(frm, cdt, cdn) {
			const row = locals[cdt][cdn];
			if (row.charge_type === "Actual") {
				frappe.model.set_value(cdt, cdn, "rate", 0);
			}
			recalculate_freight(frm);
		},
		account_head: recalculate_freight,
		description: recalculate_freight,
		rate: recalculate_freight,
		amount: recalculate_freight,
		taxable: recalculate_freight,
		cost_center: recalculate_freight,
	});

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

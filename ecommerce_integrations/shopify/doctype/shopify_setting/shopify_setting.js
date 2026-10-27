// Copyright (c) 2021, Frappe and contributors
// For license information, please see LICENSE

frappe.provide("ecommerce_integrations.shopify.shopify_setting");

frappe.ui.form.on("Shopify Setting", {
	onload: function (frm) {
		frappe.call({
			method: "ecommerce_integrations.utils.naming_series.get_series",
			callback: function (r) {
				$.each(r.message, (key, value) => {
					set_field_options(key, value);
					frm.fields_dict.company_config.grid.update_docfield_property(key, "options", value);
				});
			},
		});
	},

	fetch_shopify_locations: function (frm) {
		frappe.call({
			doc: frm.doc,
			method: "update_location_table",
			callback: (r) => {
				if (!r.exc) refresh_field("shopify_warehouse_mapping");
			},
		});
	},

	push_inventory_now: function (frm) {
		frappe.confirm(
			__(
				"Push ERPNext stock for all mapped warehouses to Shopify now? This ignores the dirty-bin gate and runs in the background."
			),
			() => {
				frappe.call({
					method: "ecommerce_integrations.shopify.inventory.push_inventory_to_shopify_now",
					freeze: true,
				});
			}
		);
	},

	refresh: function (frm) {
		frm.add_custom_button(__("Import Products"), function () {
			frappe.set_route("shopify-import-products");
		});
		frm.add_custom_button(__("View Logs"), () => {
			frappe.set_route("List", "Ecommerce Integration Log", {
				integration: "Shopify",
			});
		});
		frm.add_custom_button(__("Inventory Mismatch Report"), () => {
			frappe.set_route("query-report", "Shopify Inventory Mismatch");
		});
		frm.trigger("setup_queries");
	},

	setup_queries: function (frm) {
		const configured_companies = () =>
			(frm.doc.company_config || []).map((row) => row.company).filter(Boolean);

		frm.set_query("erpnext_warehouse", "shopify_warehouse_mapping", () => {
			const companies = configured_companies();
			const filters = {
				is_group: 0,
				disabled: 0,
			};
			if (companies.length === 1) {
				filters.company = companies[0];
			} else if (companies.length > 1) {
				filters.company = ["in", companies];
			}
			return { filters };
		});

		frm.set_query("default_warehouse", "company_config", (doc, cdt, cdn) => {
			const row = locals[cdt][cdn];
			return {
				filters: {
					company: row.company,
					is_group: 0,
					disabled: 0,
				},
			};
		});
		frm.set_query("returns_warehouse", "company_config", (doc, cdt, cdn) => {
			const row = locals[cdt][cdn];
			return {
				filters: {
					company: row.company,
					is_group: 0,
					disabled: 0,
				},
			};
		});
		frm.set_query("cost_center", "company_config", (doc, cdt, cdn) => {
			const row = locals[cdt][cdn];
			return {
				filters: {
					company: row.company,
					is_group: "No",
				},
			};
		});
		frm.set_query("cash_bank_account", "company_config", (doc, cdt, cdn) => {
			const row = locals[cdt][cdn];
			return {
				filters: [
					["Account", "account_type", "in", ["Cash", "Bank"]],
					["Account", "root_type", "=", "Asset"],
					["Account", "is_group", "=", 0],
					["Account", "company", "=", row.company],
				],
			};
		});

		const company_tax_query = (doc, cdt, cdn) => {
			const row = locals[cdt][cdn];
			return {
				query: "erpnext.controllers.queries.tax_account_query",
				filters: {
					account_type: ["Tax", "Chargeable", "Expense Account"],
					company: row.company,
				},
			};
		};

		frm.set_query("default_sales_tax_account", "company_config", company_tax_query);
		frm.set_query("default_shipping_charges_account", "company_config", company_tax_query);

		frm.set_query("company", "taxes", () => {
			const companies = configured_companies();
			if (companies.length) {
				return { filters: { name: ["in", companies] } };
			}
			return {};
		});

		frm.set_query("tax_account", "taxes", (doc, cdt, cdn) => {
			const row = locals[cdt][cdn];
			return {
				query: "erpnext.controllers.queries.tax_account_query",
				filters: {
					account_type: ["Tax", "Chargeable", "Expense Account"],
					company: row.company,
				},
			};
		});
	},
});

frappe.ui.form.on("Shopify Company Config", {
	company: function (frm, cdt, cdn) {
		frm.trigger("setup_queries");
	},
});

frappe.ui.form.on("Shopify Warehouse Mapping", {
	erpnext_warehouse: function (frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.erpnext_warehouse) {
			frappe.model.set_value(cdt, cdn, "company", "");
			return;
		}
		frappe.db.get_value("Warehouse", row.erpnext_warehouse, "company", (r) => {
			frappe.model.set_value(cdt, cdn, "company", r.company || "");
		});
	},
});

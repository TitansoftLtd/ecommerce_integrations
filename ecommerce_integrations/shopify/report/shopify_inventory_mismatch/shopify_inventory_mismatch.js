// Copyright (c) 2026, Frappe and contributors
// For license information, please see LICENSE

frappe.query_reports["Shopify Inventory Mismatch"] = {
	filters: [
		{
			fieldname: "warehouse",
			label: __("Warehouse"),
			fieldtype: "Link",
			options: "Warehouse",
			description: __(
				"Optional. Only mapped Shopify warehouses are compared. Leave blank for all mapped warehouses."
			),
			get_query: function () {
				return {
					query: "ecommerce_integrations.shopify.report.shopify_inventory_mismatch.shopify_inventory_mismatch.mapped_warehouse_query",
				};
			},
		},
		{
			fieldname: "item_code",
			label: __("Item"),
			fieldtype: "Link",
			options: "Item",
		},
		{
			fieldname: "only_mismatches",
			label: __("Only Mismatches"),
			fieldtype: "Check",
			default: 1,
		},
	],

	get_datatable_options(options) {
		return Object.assign(options, {
			checkboxColumn: true,
		});
	},

	onload: function (report) {
		report.page.add_inner_button(__("Reconcile Selected"), () => {
			const selected = get_selected_rows(report);
			if (!selected.length) {
				frappe.msgprint(__("Select at least one row."));
				return;
			}
			frappe.call({
				method: "ecommerce_integrations.shopify.inventory.reconcile_inventory_rows",
				args: { rows: selected },
				freeze: true,
			});
		});

		report.page.add_inner_button(__("Reconcile All Mismatches On Page"), () => {
			const rows = get_mismatch_rows(report);
			if (!rows.length) {
				frappe.msgprint(__("No mismatch rows on this page."));
				return;
			}
			frappe.confirm(
				__("Push ERPNext qty to Shopify for {0} mismatch row(s)?", [rows.length]),
				() => {
					frappe.call({
						method: "ecommerce_integrations.shopify.inventory.reconcile_inventory_rows",
						args: { rows },
						freeze: true,
					});
				}
			);
		});

		report.page.add_inner_button(__("Push All Stock Now"), () => {
			frappe.confirm(
				__(
					"Push ERPNext stock for all mapped warehouses to Shopify now? This runs in the background."
				),
				() => {
					frappe.call({
						method: "ecommerce_integrations.shopify.inventory.push_inventory_to_shopify_now",
						freeze: true,
					});
				}
			);
		});
	},
};

function get_report_data(report) {
	return report.data || (frappe.query_report && frappe.query_report.data) || [];
}

function get_selected_rows(report) {
	const indices =
		report.datatable && report.datatable.rowmanager
			? report.datatable.rowmanager.getCheckedRows()
			: [];
	const data = get_report_data(report);
	return indices.map((i) => data[i]).filter(Boolean);
}

function get_mismatch_rows(report) {
	return get_report_data(report).filter((r) => {
		if (!r || r.status === "Not Found") {
			return false;
		}
		if (r.status === "Mismatch") {
			return true;
		}
		return cint(r.difference) !== 0;
	});
}

function cint(v) {
	const n = parseInt(v, 10);
	return Number.isNaN(n) ? 0 : n;
}

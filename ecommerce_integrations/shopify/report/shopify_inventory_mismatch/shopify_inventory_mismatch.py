# Copyright (c) 2026, Frappe and contributors
# For license information, please see LICENSE

import frappe
from frappe import _
from frappe.desk.reportview import get_match_cond

from ecommerce_integrations.shopify.inventory import get_inventory_mismatch_data


def execute(filters=None):
	columns = get_columns()
	data = get_inventory_mismatch_data(filters)
	return columns, data


def get_columns():
	return [
		{
			"label": _("Item"),
			"fieldname": "item_code",
			"fieldtype": "Link",
			"options": "Item",
			"width": 160,
		},
		{
			"label": _("Ecommerce Item"),
			"fieldname": "ecom_item",
			"fieldtype": "Link",
			"options": "Ecommerce Item",
			"width": 140,
		},
		{
			"label": _("Variant Id"),
			"fieldname": "variant_id",
			"fieldtype": "Data",
			"width": 120,
		},
		{
			"label": _("Warehouse"),
			"fieldname": "warehouse",
			"fieldtype": "Link",
			"options": "Warehouse",
			"width": 150,
		},
		{
			"label": _("Shopify Location Id"),
			"fieldname": "shopify_location_id",
			"fieldtype": "Data",
			"width": 130,
		},
		{
			"label": _("ERP Available"),
			"fieldname": "erp_available",
			"fieldtype": "Int",
			"width": 110,
		},
		{
			"label": _("Reserved Qty"),
			"fieldname": "reserved_qty",
			"fieldtype": "Int",
			"width": 100,
		},
		{
			"label": _("Shopify Available"),
			"fieldname": "shopify_available",
			"fieldtype": "Int",
			"width": 130,
		},
		{
			"label": _("Difference (ERP − Shopify)"),
			"fieldname": "difference",
			"fieldtype": "Int",
			"width": 160,
		},
		{
			"label": _("Last Inventory Synced On"),
			"fieldname": "inventory_synced_on",
			"fieldtype": "Datetime",
			"width": 160,
		},
		{
			"label": _("Status"),
			"fieldname": "status",
			"fieldtype": "Data",
			"width": 100,
		},
	]


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def mapped_warehouse_query(doctype, txt, searchfield, start, page_len, filters):
	"""Link query: only warehouses mapped on Shopify Setting."""
	setting = frappe.get_cached_doc("Shopify Setting")
	mapped = list(setting.get_erpnext_to_integration_wh_mapping().keys())
	if not mapped:
		return []

	txt = f"%{txt}%"
	return frappe.db.sql(
		f"""
		select name, warehouse_name
		from `tabWarehouse`
		where name in %(mapped)s
			and is_group = 0
			and ifnull(disabled, 0) = 0
			and (name like %(txt)s or warehouse_name like %(txt)s)
			{get_match_cond(doctype)}
		order by name
		limit %(start)s, %(page_len)s
		""",
		{"mapped": mapped, "txt": txt, "start": start, "page_len": page_len},
	)

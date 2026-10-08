import frappe

from ecommerce_integrations.shopify.constants import SETTING_DOCTYPE


def execute():
	"""Seed Shopify Company Config from legacy single-company Shopify Setting fields."""
	frappe.reload_doc("shopify", "doctype", "shopify_company_config")
	frappe.reload_doc("shopify", "doctype", "shopify_tax_account")
	frappe.reload_doc("shopify", "doctype", "shopify_warehouse_mapping")
	frappe.reload_doc("shopify", "doctype", "shopify_setting")

	if not frappe.db.exists("DocType", SETTING_DOCTYPE):
		return

	setting = frappe.get_doc(SETTING_DOCTYPE)

	if setting.get("company_config"):
		_backfill_tax_companies(setting)
		_backfill_warehouse_mapping_companies(setting)
		setting.flags.ignore_validate = True
		setting.flags.ignore_mandatory = True
		setting.save(ignore_permissions=True)
		return

	company = setting.get("company")
	if not company:
		return

	setting.append(
		"company_config",
		{
			"company": company,
			"is_primary": 1,
			"default_warehouse": setting.get("warehouse"),
			"returns_warehouse": setting.get("shopify_returns_warehouse"),
			"cash_bank_account": setting.get("cash_bank_account"),
			"cost_center": setting.get("cost_center"),
			"default_sales_tax_account": setting.get("default_sales_tax_account"),
			"default_shipping_charges_account": setting.get("default_shipping_charges_account"),
			"sales_order_series": setting.get("sales_order_series"),
			"delivery_note_series": setting.get("delivery_note_series"),
			"sales_invoice_series": setting.get("sales_invoice_series"),
		},
	)

	_backfill_tax_companies(setting, default_company=company)
	_backfill_warehouse_mapping_companies(setting)

	setting.flags.ignore_validate = True
	setting.flags.ignore_mandatory = True
	setting.save(ignore_permissions=True)


def _backfill_tax_companies(setting, default_company=None):
	default_company = default_company or (
		next((row.company for row in (setting.company_config or []) if row.is_primary), None)
		or next((row.company for row in (setting.company_config or [])), None)
	)
	if not default_company:
		return
	for tax in setting.taxes or []:
		if not tax.company:
			tax.company = default_company


def _backfill_warehouse_mapping_companies(setting):
	for wh_map in setting.shopify_warehouse_mapping or []:
		if wh_map.erpnext_warehouse and not wh_map.company:
			wh_map.company = frappe.db.get_value("Warehouse", wh_map.erpnext_warehouse, "company")

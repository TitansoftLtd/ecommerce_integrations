# Copyright (c) 2026, Frappe and contributors
# For license information, please see LICENSE

"""Resolve per-company Shopify commercial config for sync paths."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import frappe
from frappe import _

from ecommerce_integrations.shopify.constants import SETTING_DOCTYPE

if TYPE_CHECKING:
	from frappe.model.document import Document

# Shopify assigns FulfillmentOrders asynchronously after order create.
# Wait briefly so we do not fall back to Primary and create the SO in the wrong company.
FO_LOCATION_FETCH_ATTEMPTS = 5
FO_LOCATION_FETCH_DELAY_SECONDS = 1.0


class CrossCompanyOrderError(frappe.ValidationError):
	pass


def get_configured_companies(setting: Document | None = None) -> list[str]:
	setting = setting or frappe.get_cached_doc(SETTING_DOCTYPE)
	return [row.company for row in (setting.company_config or []) if row.company]


def get_primary_company_config(setting: Document | None = None):
	setting = setting or frappe.get_cached_doc(SETTING_DOCTYPE)
	primary = None
	for row in setting.company_config or []:
		if row.is_primary:
			return row
		if primary is None:
			primary = row
	if primary:
		return primary
	return _legacy_company_config(setting)


def get_company_config(company: str, setting: Document | None = None):
	setting = setting or frappe.get_cached_doc(SETTING_DOCTYPE)
	for row in setting.company_config or []:
		if row.company == company:
			return row
	legacy = _legacy_company_config(setting)
	if legacy and legacy.company == company:
		return legacy
	frappe.throw(_("No Shopify Company Config found for company {0}").format(company))


def get_company_config_for_transaction(doc, setting: Document | None = None):
	"""Resolve company config from an ERPNext transaction (SO / SI / DN)."""
	company = getattr(doc, "company", None)
	if not company:
		frappe.throw(_("Company missing on {0}").format(doc.doctype))
	return get_company_config(company, setting)


def _legacy_company_config(setting: Document):
	"""Fallback when migration has not yet populated company_config."""
	if not setting.get("company"):
		return None
	return frappe._dict(
		{
			"company": setting.company,
			"is_primary": 1,
			"default_warehouse": setting.warehouse,
			"returns_warehouse": setting.shopify_returns_warehouse,
			"cash_bank_account": setting.cash_bank_account,
			"cost_center": setting.cost_center,
			"default_sales_tax_account": setting.default_sales_tax_account,
			"default_shipping_charges_account": setting.default_shipping_charges_account,
			"sales_order_series": setting.sales_order_series,
			"delivery_note_series": setting.delivery_note_series,
			"sales_invoice_series": setting.sales_invoice_series,
		}
	)


def collect_order_location_ids(shopify_order: dict) -> list[str]:
	"""Location IDs from fulfillments already on the order payload."""
	locations: list[str] = []
	for fulfillment in shopify_order.get("fulfillments") or []:
		location_id = fulfillment.get("location_id")
		if location_id is not None:
			locations.append(str(location_id))
	return locations


def fetch_fulfillment_order_location_ids(order_id) -> list[str]:
	"""Fetch assigned_location_id from Shopify FulfillmentOrders for an order.

	Returns empty list on failure / empty response so callers can fall back to Primary.

	Uses ``shopify.FulfillmentOrders.find`` (authenticated resource path). Raw
	``connection.get("orders/...")`` hits the storefront; absolute Admin paths via
	``connection.get`` omit the access-token header and 401.

	Requires app scope ``read_merchant_managed_fulfillment_orders`` (not only
	``read_assigned_fulfillment_orders``); without it Shopify returns an empty list.
	"""
	from ecommerce_integrations.shopify.connection import temp_shopify_session

	@temp_shopify_session
	def _fetch():
		import shopify

		locations: list[str] = []
		try:
			for fo in shopify.FulfillmentOrders.find(order_id=order_id) or []:
				assigned = getattr(fo, "assigned_location_id", None)
				if assigned is None and hasattr(fo, "to_dict"):
					assigned = (fo.to_dict() or {}).get("assigned_location_id")
				if assigned is not None:
					locations.append(str(assigned))
		except Exception:
			frappe.log_error(
				title="Shopify FulfillmentOrders fetch failed",
				message=frappe.get_traceback(),
			)
		return locations

	try:
		return _fetch()
	except Exception:
		frappe.log_error(
			title="Shopify FulfillmentOrders fetch failed",
			message=frappe.get_traceback(),
		)
		return []


def fetch_fulfillment_order_location_ids_with_retry(order_id) -> list[str]:
	"""Retry FO fetch briefly while Shopify finishes routing after orders/create."""
	for attempt in range(FO_LOCATION_FETCH_ATTEMPTS):
		locations = fetch_fulfillment_order_location_ids(order_id)
		if locations:
			return locations
		if attempt < FO_LOCATION_FETCH_ATTEMPTS - 1:
			time.sleep(FO_LOCATION_FETCH_DELAY_SECONDS)
	return []


def map_locations_to_companies(location_ids: list[str], setting: Document | None = None) -> set[str]:
	setting = setting or frappe.get_cached_doc(SETTING_DOCTYPE)
	wh_map = setting.get_integration_to_erpnext_wh_mapping()
	companies: set[str] = set()
	for location_id in location_ids:
		warehouse = wh_map.get(str(location_id))
		if not warehouse:
			continue
		company = frappe.db.get_value("Warehouse", warehouse, "company")
		if company:
			companies.add(company)
	return companies


def resolve_company_config_for_order(shopify_order: dict, setting: Document | None = None):
	"""Resolve company config for Sales Order create.

	1. Fulfillment location_ids on payload
	2. Else FulfillmentOrders assigned_location_id (with short retry for routing lag)
	3. Map to companies via warehouse mapping
	4. 0 companies → Primary; 1 → that config; 2+ → raise CrossCompanyOrderError
	"""
	setting = setting or frappe.get_doc(SETTING_DOCTYPE)

	location_ids = collect_order_location_ids(shopify_order)
	if not location_ids:
		order_id = shopify_order.get("id")
		if order_id:
			location_ids = fetch_fulfillment_order_location_ids_with_retry(order_id)

	companies = map_locations_to_companies(location_ids, setting) if location_ids else set()

	if len(companies) > 1:
		raise CrossCompanyOrderError(
			_(
				"Shopify order {0} is assigned to locations in multiple companies ({1}). "
				"Cross-company orders are not supported."
			).format(shopify_order.get("name") or shopify_order.get("id"), ", ".join(sorted(companies)))
		)

	if len(companies) == 1:
		return get_company_config(next(iter(companies)), setting), location_ids

	return get_primary_company_config(setting), location_ids


def default_line_warehouse(company_config, location_ids: list[str] | None = None, setting=None) -> str:
	"""Single warehouse for SO lines when only one location/company is known."""
	setting = setting or frappe.get_cached_doc(SETTING_DOCTYPE)
	if location_ids:
		wh_map = setting.get_integration_to_erpnext_wh_mapping()
		for location_id in location_ids:
			warehouse = wh_map.get(str(location_id))
			if warehouse:
				return warehouse
	return company_config.default_warehouse

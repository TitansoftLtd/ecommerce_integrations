# Copyright (c) 2026, Frappe and contributors
# For license information, please see LICENSE

"""Push ERPNext Item Prices to Shopify catalog / market price lists."""

from __future__ import annotations

import json

import frappe
import shopify
from frappe import _
from frappe.utils import cint, cstr, flt

from ecommerce_integrations.shopify.connection import temp_shopify_session
from ecommerce_integrations.shopify.constants import MODULE_NAME, SETTING_DOCTYPE
from ecommerce_integrations.shopify.utils import create_shopify_log

CATALOGS_QUERY = """
query ($cursor: String) {
  catalogs(first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      title
      status
      priceList {
        id
        name
        currency
      }
    }
  }
}
"""

PRICE_LIST_FIXED_PRICES_ADD = """
mutation priceListFixedPricesAdd($priceListId: ID!, $prices: [PriceListPriceInput!]!) {
  priceListFixedPricesAdd(priceListId: $priceListId, prices: $prices) {
    prices { variant { id } }
    userErrors { field code message }
  }
}
"""

PRICE_LIST_FIXED_PRICES_DELETE = """
mutation priceListFixedPricesDelete($priceListId: ID!, $variantIds: [ID!]!) {
  priceListFixedPricesDelete(priceListId: $priceListId, variantIds: $variantIds) {
    deletedFixedPriceVariantIds
    userErrors { field code message }
  }
}
"""

BATCH_SIZE = 250


def normalize_currency(code: str | None) -> str:
	return (code or "").strip().upper()


def variant_gid(variant_id: str | int | None) -> str | None:
	variant_id = cstr(variant_id).strip()
	if not variant_id:
		return None
	if variant_id.startswith("gid://"):
		return variant_id
	return f"gid://shopify/ProductVariant/{variant_id}"


def get_shopify_variant_id(item_code: str) -> str | None:
	"""Numeric Shopify variant id for a synced non-template Ecommerce Item."""
	return frappe.db.get_value(
		"Ecommerce Item",
		{
			"erpnext_item_code": item_code,
			"integration": MODULE_NAME,
			"has_variants": 0,
		},
		"variant_id",
	)


def parse_catalog_nodes(payload: dict) -> list[dict]:
	"""Flatten GraphQL catalogs connection into map-ready dicts."""
	nodes = (((payload or {}).get("data") or {}).get("catalogs") or {}).get("nodes") or []
	rows = []
	for node in nodes:
		if not isinstance(node, dict) or not node.get("id"):
			continue
		price_list = node.get("priceList") or {}
		rows.append(
			{
				"shopify_catalog_id": cstr(node.get("id")),
				"catalog_title": cstr(node.get("title") or ""),
				"price_list_id": cstr(price_list.get("id") or ""),
				"currency": normalize_currency(price_list.get("currency")),
				"status": cstr(node.get("status") or ""),
			}
		)
	return rows


@temp_shopify_session
def fetch_shopify_catalogs() -> list[dict]:
	"""Paginate Admin GraphQL catalogs; return parsed rows."""
	collected: list[dict] = []
	cursor = None
	while True:
		raw = shopify.GraphQL().execute(CATALOGS_QUERY, variables={"cursor": cursor})
		data = json.loads(raw) if isinstance(raw, str) else raw
		errors = data.get("errors") if isinstance(data, dict) else None
		if errors:
			frappe.throw(_("Shopify catalogs GraphQL error: {0}").format(errors))

		collected.extend(parse_catalog_nodes(data))
		page_info = (((data or {}).get("data") or {}).get("catalogs") or {}).get("pageInfo") or {}
		if not page_info.get("hasNextPage"):
			break
		cursor = page_info.get("endCursor")
		if not cursor:
			break
	return collected


def _catalog_map_rows(setting) -> list:
	if hasattr(setting, "get"):
		return setting.get("shopify_catalog_map") or []
	return getattr(setting, "shopify_catalog_map", None) or []


def get_enabled_catalog_maps(setting=None) -> list:
	setting = setting or frappe.get_doc(SETTING_DOCTYPE)
	rows = []
	for row in _catalog_map_rows(setting):
		if not cint(row.enabled):
			continue
		if not row.price_list_id or not row.erpnext_price_list:
			continue
		rows.append(row)
	return rows


def get_catalog_maps_for_price_list(price_list: str, setting=None) -> list:
	price_list = cstr(price_list)
	return [row for row in get_enabled_catalog_maps(setting) if cstr(row.erpnext_price_list) == price_list]


def validate_catalog_map_currencies(setting) -> None:
	"""Throw when ERPNext Price List currency differs from Shopify price list currency."""
	for row in _catalog_map_rows(setting):
		if not row.erpnext_price_list or not row.currency:
			continue
		erp_currency = normalize_currency(
			frappe.db.get_value("Price List", row.erpnext_price_list, "currency")
		)
		shop_currency = normalize_currency(row.currency)
		if erp_currency and shop_currency and erp_currency != shop_currency:
			frappe.throw(
				_(
					"Catalog {0}: ERPNext Price List {1} currency ({2}) does not match "
					"Shopify price list currency ({3})."
				).format(row.catalog_title or row.shopify_catalog_id, row.erpnext_price_list, erp_currency, shop_currency)
			)


def get_item_price_rate(item_code: str, price_list: str) -> float | None:
	rate = frappe.db.get_value(
		"Item Price",
		{"item_code": item_code, "price_list": price_list, "selling": 1},
		"price_list_rate",
	)
	if rate is None:
		rate = frappe.db.get_value(
			"Item Price",
			{"item_code": item_code, "price_list": price_list},
			"price_list_rate",
		)
	if rate is None:
		return None
	return flt(rate)


def _graphql(query: str, variables: dict | None = None) -> dict:
	raw = shopify.GraphQL().execute(query, variables=variables or {})
	data = json.loads(raw) if isinstance(raw, str) else raw
	if not isinstance(data, dict):
		frappe.throw(_("Unexpected Shopify GraphQL response"))
	if data.get("errors"):
		frappe.throw(_("Shopify GraphQL error: {0}").format(data.get("errors")))
	return data


def _raise_user_errors(payload_key: str, data: dict) -> None:
	body = ((data.get("data") or {}).get(payload_key)) or {}
	user_errors = body.get("userErrors") or []
	if user_errors:
		frappe.throw(_("Shopify price list error: {0}").format(user_errors))


@temp_shopify_session
def add_fixed_prices(price_list_id: str, prices: list[dict]) -> None:
	"""prices: list of {variantId, price: {amount, currencyCode}}"""
	if not prices:
		return
	for i in range(0, len(prices), BATCH_SIZE):
		chunk = prices[i : i + BATCH_SIZE]
		data = _graphql(
			PRICE_LIST_FIXED_PRICES_ADD,
			{"priceListId": price_list_id, "prices": chunk},
		)
		_raise_user_errors("priceListFixedPricesAdd", data)


@temp_shopify_session
def delete_fixed_prices(price_list_id: str, variant_ids: list[str]) -> None:
	if not variant_ids:
		return
	for i in range(0, len(variant_ids), BATCH_SIZE):
		chunk = variant_ids[i : i + BATCH_SIZE]
		data = _graphql(
			PRICE_LIST_FIXED_PRICES_DELETE,
			{"priceListId": price_list_id, "variantIds": chunk},
		)
		_raise_user_errors("priceListFixedPricesDelete", data)


def build_fixed_price_input(variant_id: str, amount: float, currency: str) -> dict:
	return {
		"variantId": variant_gid(variant_id),
		"price": {
			"amount": f"{flt(amount):.2f}",
			"currencyCode": normalize_currency(currency),
		},
	}


def push_catalog_prices_for_item(item_code: str, setting=None) -> dict:
	"""Push all mapped catalog prices for one synced item. Returns counts."""
	setting = setting or frappe.get_doc(SETTING_DOCTYPE)
	result = {"pushed": 0, "skipped": 0, "failed": 0}

	if not setting.is_enabled():
		return result

	variant_id = get_shopify_variant_id(item_code)
	if not variant_id:
		result["skipped"] += 1
		return result

	maps = get_enabled_catalog_maps(setting)
	if not maps:
		return result

	for row in maps:
		rate = get_item_price_rate(item_code, row.erpnext_price_list)
		if rate is None:
			result["skipped"] += 1
			continue
		try:
			add_fixed_prices(
				row.price_list_id,
				[build_fixed_price_input(variant_id, rate, row.currency)],
			)
			result["pushed"] += 1
		except Exception:
			result["failed"] += 1
			frappe.log_error(
				title=f"Shopify catalog price push failed ({item_code})",
				message=frappe.get_traceback(),
			)
	return result


def _should_skip_item_price_hook() -> bool:
	return bool(
		frappe.flags.in_install
		or frappe.flags.in_patch
		or frappe.flags.in_migrate
		or frappe.flags.in_import
	)


def on_item_price_update(doc, method=None):
	"""Item Price after_insert / on_update → fixed price add for mapped catalogs."""
	if _should_skip_item_price_hook():
		return
	_sync_item_price_to_catalogs(doc, delete=False)


def on_item_price_trash(doc, method=None):
	"""Item Price on_trash → remove fixed prices on mapped Shopify price lists."""
	if _should_skip_item_price_hook():
		return
	_sync_item_price_to_catalogs(doc, delete=True)


def _sync_item_price_to_catalogs(doc, *, delete: bool) -> None:
	setting = frappe.get_cached_doc(SETTING_DOCTYPE)
	if not setting.is_enabled():
		return

	maps = get_catalog_maps_for_price_list(doc.price_list, setting)
	if not maps:
		return

	variant_id = get_shopify_variant_id(doc.item_code)
	if not variant_id:
		return

	gid = variant_gid(variant_id)
	for row in maps:
		try:
			if delete:
				delete_fixed_prices(row.price_list_id, [gid])
			else:
				rate = flt(doc.price_list_rate)
				add_fixed_prices(
					row.price_list_id,
					[build_fixed_price_input(variant_id, rate, row.currency)],
				)
		except Exception:
			frappe.log_error(
				title=f"Shopify catalog price {'delete' if delete else 'update'} failed",
				message=frappe.get_traceback(),
			)


@frappe.whitelist()
def push_catalog_prices_now() -> None:
	"""Desk button: enqueue bulk catalog price push."""
	if not frappe.has_permission(SETTING_DOCTYPE, "write"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	setting = frappe.get_doc(SETTING_DOCTYPE)
	if not setting.is_enabled():
		frappe.throw(_("Shopify integration is disabled."))

	if not get_enabled_catalog_maps(setting):
		frappe.throw(
			_(
				"No enabled catalog maps with both Shopify Price List ID and ERPNext Price List. "
				"Fetch catalogs and map price lists first."
			)
		)

	frappe.enqueue(
		method="ecommerce_integrations.shopify.catalog_prices.run_catalog_price_push",
		queue="long",
		timeout=3600,
		enqueue_after_commit=True,
	)
	frappe.msgprint(
		_("Pushing catalog prices to Shopify in the background. Check Ecommerce Integration Log when finished."),
		indicator="blue",
		alert=True,
	)


def run_catalog_price_push() -> None:
	setting = frappe.get_doc(SETTING_DOCTYPE)
	if not setting.is_enabled():
		return

	maps = get_enabled_catalog_maps(setting)
	if not maps:
		create_shopify_log(
			method="push_catalog_prices_now",
			status="Invalid",
			message="No enabled catalog maps configured",
		)
		return

	pushed = skipped = failed = 0

	for row in maps:
		item_prices = frappe.get_all(
			"Item Price",
			filters={"price_list": row.erpnext_price_list},
			fields=["item_code", "price_list_rate"],
		)
		prices_payload: list[dict] = []
		for ip in item_prices:
			if flt(ip.price_list_rate) < 0:
				skipped += 1
				continue
			variant_id = get_shopify_variant_id(ip.item_code)
			if not variant_id:
				skipped += 1
				continue
			prices_payload.append(
				build_fixed_price_input(variant_id, ip.price_list_rate, row.currency)
			)

		if not prices_payload:
			continue

		try:
			add_fixed_prices(row.price_list_id, prices_payload)
			pushed += len(prices_payload)
		except Exception:
			failed += len(prices_payload)
			frappe.log_error(
				title=f"Shopify bulk catalog price push failed ({row.catalog_title})",
				message=frappe.get_traceback(),
			)

	status = "Error" if failed and not pushed else "Success"
	create_shopify_log(
		method="push_catalog_prices_now",
		status=status,
		message=_("Catalog price push finished. Pushed: {0}, Skipped: {1}, Failed: {2}").format(
			pushed, skipped, failed
		),
		make_new=True,
	)

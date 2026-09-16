# Copyright (c) 2026, Frappe and contributors
# For license information, please see LICENSE

import json
from collections import defaultdict

import frappe
import shopify
from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_return
from frappe.utils import cint, cstr, flt, getdate, nowdate

from ecommerce_integrations.shopify.connection import temp_shopify_session
from ecommerce_integrations.shopify.constants import (
	ORDER_ID_FIELD,
	ORDER_NUMBER_FIELD,
	RETURN_ID_FIELD,
	SETTING_DOCTYPE,
)
from ecommerce_integrations.shopify.product import get_item_code
from ecommerce_integrations.shopify.utils import create_shopify_log

RETURN_GRAPHQL = """
query getReturn($id: ID!) {
  return(id: $id) {
    id
    status
    name
    order {
      legacyResourceId
      name
    }
    returnLineItems(first: 100) {
      nodes {
        quantity
        fulfillmentLineItem {
          lineItem {
            sku
            variant { legacyResourceId }
            product { legacyResourceId }
          }
        }
      }
    }
  }
}
"""


def prepare_return_request(payload, request_id=None):
	"""Create-return only: no stock until process/close or refund restock."""
	frappe.set_user("Administrator")
	frappe.flags.request_id = request_id
	create_shopify_log(
		status="Success",
		message="Return requested in Shopify; ERPNext return Delivery Note is created on process/close or refund restock.",
	)


def prepare_return(payload, request_id=None):
	frappe.set_user("Administrator")
	setting = frappe.get_doc(SETTING_DOCTYPE)
	frappe.flags.request_id = request_id

	try:
		if not cint(setting.sync_shopify_returns):
			create_shopify_log(status="Invalid", message="Return sync is disabled.")
			return

		sync_return(payload, setting)
		create_shopify_log(status="Success")
	except Exception as e:
		create_shopify_log(status="Error", exception=e, rollback=True)


def sync_return(payload, setting):
	return_id = cstr(payload.get("id"))
	order_id = _extract_order_id(payload)
	qty_by_item = get_return_qty_by_item_code(payload)

	if (not qty_by_item or not order_id) and return_id:
		fetched = fetch_return_details(return_id, payload.get("admin_graphql_api_id"))
		if fetched:
			order_id = order_id or fetched.get("order_id")
			qty_by_item = qty_by_item or fetched.get("qty_by_item") or {}

	if not order_id:
		frappe.throw("Shopify order id missing on return payload")

	if not qty_by_item:
		frappe.throw(f"No returnable line items found for Shopify return {return_id}")

	create_return_delivery_notes(
		order_id=order_id,
		return_id=return_id,
		qty_by_item=qty_by_item,
		setting=setting,
		order_number=payload.get("name") or (payload.get("order") or {}).get("name"),
	)


def create_return_delivery_note_from_refund(refund, setting):
	"""Refund-driven restock (restock_type=return), including Process and refund without prior return DN."""
	refund_id = cstr(refund.get("id"))
	order_id = cstr(refund.get("order_id"))
	return_obj = refund.get("return") or {}
	return_id = cstr(return_obj.get("id") or f"refund-restock-{refund_id}")

	qty_by_item: dict[str, float] = defaultdict(float)
	for row in refund.get("refund_line_items") or []:
		if row.get("restock_type") != "return":
			continue
		line_item = row.get("line_item") or {}
		item_code = get_item_code(line_item)
		if item_code:
			qty_by_item[item_code] += flt(row.get("quantity"))

	if not qty_by_item:
		return

	create_return_delivery_notes(
		order_id=order_id,
		return_id=return_id,
		qty_by_item=dict(qty_by_item),
		setting=setting,
		posting_date=getdate(refund.get("created_at")) or nowdate(),
	)


def create_return_delivery_notes(order_id, return_id, qty_by_item, setting, order_number=None, posting_date=None):
	warehouse = setting.shopify_returns_warehouse
	if not warehouse:
		frappe.throw("Shopify Returns Warehouse is not configured")

	# Already processed under this return id (any DN)
	if frappe.db.exists("Delivery Note", {RETURN_ID_FIELD: return_id, "docstatus": 1, "is_return": 1}):
		return

	source_dns = frappe.get_all(
		"Delivery Note",
		filters={ORDER_ID_FIELD: order_id, "docstatus": 1, "is_return": 0},
		pluck="name",
		order_by="creation asc",
	)
	if not source_dns:
		frappe.throw(f"Delivery Note not found for Shopify order {order_id}")

	remaining = defaultdict(float, qty_by_item)
	posting_date = posting_date or nowdate()
	created = False
	base_return_id = return_id

	for idx, dn_name in enumerate(source_dns):
		if not any(qty > 0 for qty in remaining.values()):
			break

		row_return_id = base_return_id if idx == 0 else f"{base_return_id}:{dn_name}"
		if frappe.db.exists(
			"Delivery Note", {RETURN_ID_FIELD: row_return_id, "docstatus": 1, "is_return": 1}
		):
			continue

		dn_return = make_sales_return(dn_name)
		kept = []
		for item in dn_return.items:
			need = remaining.get(item.item_code, 0)
			if need <= 0:
				continue
			take = min(abs(flt(item.qty)), need)
			if take <= 0:
				continue
			item.qty = -take
			item.warehouse = warehouse
			remaining[item.item_code] -= take
			kept.append(item)

		if not kept:
			continue

		dn_return.items = kept
		dn_return.set(RETURN_ID_FIELD, row_return_id)
		dn_return.set(ORDER_ID_FIELD, order_id)
		if order_number:
			dn_return.set(ORDER_NUMBER_FIELD, order_number)
		else:
			dn_return.set(
				ORDER_NUMBER_FIELD,
				frappe.db.get_value("Delivery Note", dn_name, ORDER_NUMBER_FIELD),
			)
		dn_return.naming_series = setting.delivery_note_series or dn_return.naming_series
		dn_return.set_posting_time = 1
		dn_return.posting_date = posting_date
		dn_return.flags.ignore_mandatory = True
		dn_return.save()
		dn_return.submit()
		created = True

	if not created:
		# Likely already returned via the other webhook path (refund restock vs returns/process)
		return


def get_return_qty_by_item_code(payload) -> dict[str, float]:
	qty_map: dict[str, float] = defaultdict(float)
	for row in payload.get("return_line_items") or []:
		line_item = row.get("line_item") or {}
		# Some payloads nest under fulfillment_line_item
		if not line_item and row.get("fulfillment_line_item"):
			line_item = (row.get("fulfillment_line_item") or {}).get("line_item") or {}
		item_code = get_item_code(line_item) if line_item else None
		if not item_code and line_item:
			# GraphQL-shaped dicts already converted
			item_code = get_item_code(
				{
					"product_id": line_item.get("product_id"),
					"variant_id": line_item.get("variant_id"),
					"sku": line_item.get("sku"),
				}
			)
		qty = flt(row.get("quantity") or row.get("returnable_quantity"))
		if item_code and qty:
			qty_map[item_code] += qty
	return dict(qty_map)


def _extract_order_id(payload) -> str:
	if payload.get("order_id"):
		return cstr(payload.get("order_id"))
	order = payload.get("order") or {}
	if order.get("id"):
		return cstr(order.get("id"))
	return ""


@temp_shopify_session
def fetch_return_details(return_id, admin_graphql_api_id=None) -> dict | None:
	"""Fetch return line items via GraphQL when webhook payload is sparse."""
	gid = admin_graphql_api_id or f"gid://shopify/Return/{return_id}"
	try:
		response = shopify.GraphQL().execute(RETURN_GRAPHQL, variables={"id": gid})
		data = json.loads(response) if isinstance(response, str) else response
	except Exception:
		frappe.log_error(title="Shopify Return GraphQL fetch failed")
		return None

	errors = data.get("errors") if isinstance(data, dict) else None
	if errors:
		frappe.log_error(message=str(errors), title="Shopify Return GraphQL errors")
		return None

	ret = ((data or {}).get("data") or {}).get("return")
	if not ret:
		return None

	qty_map: dict[str, float] = defaultdict(float)
	nodes = ((ret.get("returnLineItems") or {}).get("nodes")) or []
	for node in nodes:
		qty = flt(node.get("quantity"))
		line_item = ((node.get("fulfillmentLineItem") or {}).get("lineItem")) or {}
		product_id = (line_item.get("product") or {}).get("legacyResourceId")
		variant_id = (line_item.get("variant") or {}).get("legacyResourceId")
		item_code = get_item_code(
			{
				"product_id": product_id,
				"variant_id": variant_id,
				"sku": line_item.get("sku"),
			}
		)
		if item_code and qty:
			qty_map[item_code] += qty

	order = ret.get("order") or {}
	return {
		"order_id": cstr(order.get("legacyResourceId")),
		"order_number": order.get("name"),
		"qty_by_item": dict(qty_map),
	}

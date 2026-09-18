# Copyright (c) 2021, Frappe and contributors
# For license information, please see LICENSE

from collections import Counter

import frappe
from frappe import _
from frappe.utils import cint, create_batch, now
from pyactiveresource.connection import ResourceNotFound
from shopify.collection import PaginatedIterator
from shopify.resources import InventoryLevel, Variant

from ecommerce_integrations.controllers.inventory import (
	get_all_inventory_levels,
	get_inventory_levels,
	update_inventory_sync_status,
)
from ecommerce_integrations.controllers.scheduling import need_to_run
from ecommerce_integrations.shopify.connection import temp_shopify_session
from ecommerce_integrations.shopify.constants import MODULE_NAME, SETTING_DOCTYPE
from ecommerce_integrations.shopify.utils import create_shopify_log

UPLOAD_BATCH_SIZE = 50

# Roles allowed to force-push / reconcile Shopify inventory (must match report access intent)
INVENTORY_RECONCILE_ROLES = ("System Manager", "Stock Manager")


def _check_inventory_reconcile_permission():
	frappe.only_for(INVENTORY_RECONCILE_ROLES, message=True)


def update_inventory_on_shopify() -> None:
	"""Upload stock levels from ERPNext to Shopify.

	Called by scheduler on configured interval. Only dirty bins (Bin modified
	after last inventory sync on Ecommerce Item).
	"""
	setting = frappe.get_doc(SETTING_DOCTYPE)

	if not setting.is_enabled() or not setting.update_erpnext_stock_levels_to_shopify:
		return

	if not need_to_run(SETTING_DOCTYPE, "inventory_sync_frequency", "last_inventory_sync"):
		return

	warehous_map = setting.get_erpnext_to_integration_wh_mapping()
	if not warehous_map:
		return

	inventory_levels = get_inventory_levels(tuple(warehous_map.keys()), MODULE_NAME)

	if inventory_levels:
		upload_inventory_data_to_shopify(inventory_levels, warehous_map)


@frappe.whitelist()
def push_inventory_to_shopify_now() -> None:
	"""Force-push ERPNext stock for all mapped warehouses (ignores dirty-bin gate)."""
	_check_inventory_reconcile_permission()
	setting = frappe.get_doc(SETTING_DOCTYPE)

	if not setting.is_enabled():
		frappe.throw(_("Shopify integration is disabled."))

	if not setting.update_erpnext_stock_levels_to_shopify:
		frappe.throw(_("Enable Update ERPNext stock levels to Shopify first."))

	warehous_map = setting.get_erpnext_to_integration_wh_mapping()
	if not warehous_map:
		frappe.throw(_("Map at least one Shopify location to an ERPNext warehouse."))

	frappe.enqueue(
		method="ecommerce_integrations.shopify.inventory.run_force_inventory_push",
		queue="long",
		timeout=3600,
		enqueue_after_commit=True,
	)
	frappe.msgprint(
		_("Pushing stock to Shopify in the background. Check Ecommerce Integration Log when finished."),
		indicator="blue",
		alert=True,
	)


def run_force_inventory_push() -> None:
	setting = frappe.get_doc(SETTING_DOCTYPE)
	if not setting.is_enabled() or not setting.update_erpnext_stock_levels_to_shopify:
		return

	warehous_map = setting.get_erpnext_to_integration_wh_mapping()
	if not warehous_map:
		create_shopify_log(
			method="push_inventory_to_shopify_now",
			status="Invalid",
			message="No warehouse mapping configured",
		)
		return

	inventory_levels = get_all_inventory_levels(tuple(warehous_map.keys()), MODULE_NAME)
	if not inventory_levels:
		create_shopify_log(
			method="push_inventory_to_shopify_now",
			status="Success",
			message="No linked inventory rows to push for mapped warehouses",
		)
		return

	upload_inventory_data_to_shopify(
		inventory_levels, warehous_map, method="push_inventory_to_shopify_now"
	)


@frappe.whitelist()
def reconcile_inventory_rows(rows: str | list | None = None) -> None:
	"""Force-push selected mismatch rows (from report) to Shopify."""
	_check_inventory_reconcile_permission()
	setting = frappe.get_doc(SETTING_DOCTYPE)

	if not setting.is_enabled():
		frappe.throw(_("Shopify integration is disabled."))
	if not setting.update_erpnext_stock_levels_to_shopify:
		frappe.throw(_("Enable Update ERPNext stock levels to Shopify first."))

	if isinstance(rows, str):
		import json

		rows = json.loads(rows)

	if not rows:
		frappe.throw(_("Select at least one row to reconcile."))

	warehous_map = setting.get_erpnext_to_integration_wh_mapping()
	payload = []
	for row in rows:
		warehouse = row.get("warehouse")
		if warehouse not in warehous_map:
			continue
		variant_id = cstr_variant(row.get("variant_id"))
		ecom_item = row.get("ecom_item")
		item_code = row.get("item_code")
		if not variant_id or not ecom_item or not item_code:
			continue
		payload.append(
			{
				"ecom_item": ecom_item,
				"item_code": item_code,
				"variant_id": variant_id,
				"warehouse": warehouse,
			}
		)

	if not payload:
		frappe.throw(_("No selected rows belong to a mapped warehouse with a variant id."))

	frappe.enqueue(
		method="ecommerce_integrations.shopify.inventory.run_reconcile_inventory_rows",
		queue="long",
		timeout=3600,
		enqueue_after_commit=True,
		rows=payload,
	)
	frappe.msgprint(
		_("Reconciling {0} row(s) to Shopify in the background.").format(len(payload)),
		indicator="blue",
		alert=True,
	)


def run_reconcile_inventory_rows(rows: list) -> None:
	setting = frappe.get_doc(SETTING_DOCTYPE)
	warehous_map = setting.get_erpnext_to_integration_wh_mapping()
	levels = []
	for row in rows:
		row = frappe._dict(row)
		if row.warehouse not in warehous_map:
			continue
		# Refresh ERP qty from Bin so we push current truth, not a stale report cell
		bin_qty = frappe.db.get_value(
			"Bin",
			{"item_code": row.item_code, "warehouse": row.warehouse},
			["actual_qty", "reserved_qty"],
			as_dict=True,
		)
		if not bin_qty:
			continue
		levels.append(
			frappe._dict(
				{
					"ecom_item": row.ecom_item,
					"item_code": row.item_code,
					"variant_id": row.variant_id,
					"warehouse": row.warehouse,
					"actual_qty": bin_qty.actual_qty,
					"reserved_qty": bin_qty.reserved_qty,
				}
			)
		)

	if not levels:
		create_shopify_log(
			method="reconcile_inventory_rows",
			status="Invalid",
			message="No valid rows to reconcile",
		)
		return

	upload_inventory_data_to_shopify(levels, warehous_map, method="reconcile_inventory_rows")


def cstr_variant(value) -> str:
	from frappe.utils import cstr

	return cstr(value)


@temp_shopify_session
def upload_inventory_data_to_shopify(
	inventory_levels, warehous_map, method: str = "update_inventory_on_shopify"
) -> None:
	synced_on = now()
	variant_inventory_cache: dict[str, str | None] = {}

	for inventory_sync_batch in create_batch(inventory_levels, UPLOAD_BATCH_SIZE):
		for d in inventory_sync_batch:
			d.shopify_location_id = warehous_map[d.warehouse]
			erp_available = (
				cint(d.erp_available)
				if d.get("erp_available") is not None
				else cint(d.actual_qty) - cint(d.reserved_qty)
			)
			if erp_available < 0:
				erp_available = 0

			try:
				inventory_id = _get_inventory_item_id(d.variant_id, variant_inventory_cache)
				if not inventory_id:
					raise ResourceNotFound("inventory_item_id")

				InventoryLevel.set(
					location_id=d.shopify_location_id,
					inventory_item_id=inventory_id,
					available=erp_available,
				)
				update_inventory_sync_status(d.ecom_item, time=synced_on)
				d.status = "Success"
			except ResourceNotFound:
				update_inventory_sync_status(d.ecom_item, time=synced_on)
				d.status = "Not Found"
			except Exception as e:
				d.status = "Failed"
				d.failure_reason = str(e)

			frappe.db.commit()

		_log_inventory_update_status(inventory_sync_batch, method=method)


def _get_inventory_item_id(variant_id, cache: dict) -> str | None:
	variant_id = str(variant_id)
	if variant_id in cache:
		return cache[variant_id]
	try:
		variant = Variant.find(variant_id)
		inventory_id = str(variant.inventory_item_id) if variant and variant.inventory_item_id else None
	except ResourceNotFound:
		inventory_id = None
	cache[variant_id] = inventory_id
	return inventory_id


def _log_inventory_update_status(inventory_levels, method: str = "update_inventory_on_shopify") -> None:
	"""Create log of inventory update."""
	log_message = "variant_id,location_id,status,failure_reason\n"

	log_message += "\n".join(
		f"{d.variant_id},{d.shopify_location_id},{d.status},{d.failure_reason or ''}"
		for d in inventory_levels
	)

	stats = Counter([d.status for d in inventory_levels])

	percent_successful = stats["Success"] / len(inventory_levels) if inventory_levels else 0

	if percent_successful == 0:
		status = "Failed"
	elif percent_successful < 1:
		status = "Partial Success"
	else:
		status = "Success"

	log_message = f"Updated {percent_successful * 100}% items\n\n" + log_message

	create_shopify_log(method=method, status=status, message=log_message)


@temp_shopify_session
def get_inventory_mismatch_data(filters: dict | None = None) -> list[dict]:
	"""Compare ERPNext published qty vs Shopify for mapped warehouses only."""
	filters = filters or {}
	setting = frappe.get_doc(SETTING_DOCTYPE)
	if not setting.is_enabled():
		frappe.throw(_("Shopify integration is disabled."))

	warehous_map = setting.get_erpnext_to_integration_wh_mapping()
	if not warehous_map:
		return []

	warehouse_filter = filters.get("warehouse")
	if warehouse_filter:
		if warehouse_filter not in warehous_map:
			frappe.throw(_("Warehouse {0} is not mapped to a Shopify location.").format(warehouse_filter))
		warehous_map = {warehouse_filter: warehous_map[warehouse_filter]}

	erp_rows = get_all_inventory_levels(tuple(warehous_map.keys()), MODULE_NAME)
	item_filter = filters.get("item_code")
	if item_filter:
		erp_rows = [d for d in erp_rows if d.item_code == item_filter]

	# Shopify available by location + inventory_item_id (bulk per location)
	shopify_by_location: dict[str, dict[str, int]] = {}
	for location_id in set(warehous_map.values()):
		shopify_by_location[str(location_id)] = _fetch_location_inventory_levels(str(location_id))

	variant_inventory_cache: dict[str, str | None] = {}
	only_mismatches = cint(filters.get("only_mismatches", 1))
	rows = []

	for d in erp_rows:
		location_id = str(warehous_map[d.warehouse])
		erp_available = cint(d.actual_qty) - cint(d.reserved_qty)
		if erp_available < 0:
			erp_available = 0

		inventory_id = _get_inventory_item_id(d.variant_id, variant_inventory_cache)
		shopify_available = None
		status = "OK"
		if not inventory_id:
			status = "Not Found"
		else:
			loc_map = shopify_by_location.get(location_id) or {}
			if inventory_id not in loc_map:
				status = "Not Found"
			else:
				shopify_available = cint(loc_map[inventory_id])
				if shopify_available != erp_available:
					status = "Mismatch"

		if only_mismatches and status == "OK":
			continue

		difference = None
		if shopify_available is not None:
			difference = erp_available - shopify_available

		rows.append(
			{
				"item_code": d.item_code,
				"ecom_item": d.ecom_item,
				"variant_id": d.variant_id,
				"warehouse": d.warehouse,
				"shopify_location_id": location_id,
				"erp_available": erp_available,
				"reserved_qty": cint(d.reserved_qty),
				"shopify_available": shopify_available,
				"difference": difference,
				"inventory_synced_on": d.get("inventory_synced_on"),
				"status": status,
			}
		)

	return rows


def _fetch_location_inventory_levels(location_id: str) -> dict[str, int]:
	"""Return map of inventory_item_id -> available for one Shopify location."""
	levels: dict[str, int] = {}
	try:
		for page in PaginatedIterator(InventoryLevel.find(location_ids=location_id, limit=250)):
			for level in page:
				inv_id = str(getattr(level, "inventory_item_id", "") or "")
				if not inv_id:
					continue
				levels[inv_id] = cint(getattr(level, "available", 0) or 0)
	except Exception:
		frappe.log_error(
			title=f"Shopify inventory levels fetch failed ({location_id})",
			message=frappe.get_traceback(),
		)
	return levels

"""One-off: export Shopify product + variant IDs to a public File (browser download).

Usage:
  bench --site <site> execute ecommerce_integrations.shopify.export_shopify_variant_ids.execute

Opens in Desk → File list, or download via the printed /files/... URL.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime

import frappe
from frappe.utils import get_url
from shopify.collection import PaginatedIterator
from shopify.resources import Product

from ecommerce_integrations.shopify.connection import temp_shopify_session
from ecommerce_integrations.shopify.constants import SETTING_DOCTYPE

CSV_COLUMNS = [
	"product_title",
	"integration_item_code",
	"variant_id",
	"handle",
	"sku",
	"option1",
	"option2",
	"option3",
]


@temp_shopify_session
def _fetch_rows() -> list[dict]:
	rows: list[dict] = []
	for page in PaginatedIterator(Product.find(limit=250)):
		for product in page:
			product_id = str(getattr(product, "id", "") or "")
			title = getattr(product, "title", "") or ""
			handle = getattr(product, "handle", "") or ""
			variants = getattr(product, "variants", None) or []
			for variant in variants:
				rows.append(
					{
						"product_title": title,
						"integration_item_code": product_id,
						"variant_id": str(getattr(variant, "id", "") or ""),
						"handle": handle,
						"sku": getattr(variant, "sku", "") or "",
						"option1": getattr(variant, "option1", "") or "",
						"option2": getattr(variant, "option2", "") or "",
						"option3": getattr(variant, "option3", "") or "",
					}
				)
	return rows


def _rows_to_csv_bytes(rows: list[dict]) -> bytes:
	buf = io.StringIO()
	writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS)
	writer.writeheader()
	writer.writerows(rows)
	return buf.getvalue().encode("utf-8")


def execute() -> str:
	"""Fetch all Shopify variants, save as a public File, return download URL."""
	from frappe.utils.file_manager import save_file

	rows = _fetch_rows()
	content = _rows_to_csv_bytes(rows)
	stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
	filename = f"shopify_variant_ids_{stamp}.csv"

	file_doc = save_file(
		fname=filename,
		content=content,
		dt=SETTING_DOCTYPE,
		dn=SETTING_DOCTYPE,
		is_private=0,
	)
	frappe.db.commit()

	file_url = file_doc.file_url  # e.g. /files/shopify_variant_ids_....csv
	download_url = get_url(file_url)
	msg = f"Wrote {len(rows)} variant row(s). Download: {download_url}"
	frappe.msgprint(msg, title="Shopify variant ID export", indicator="green")
	print(msg)
	return download_url

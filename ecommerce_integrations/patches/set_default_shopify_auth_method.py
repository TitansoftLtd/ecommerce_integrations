import frappe

from ecommerce_integrations.shopify.constants import SETTING_DOCTYPE


def execute():
	"""
	Migration patch to set default authentication method for existing Shopify installations.
	This ensures backward compatibility when introducing OAuth 2.0 support.
	"""
	if not frappe.db.exists("DocType", SETTING_DOCTYPE):
		return

	if not frappe.db.table_exists(SETTING_DOCTYPE):
		return

	if not frappe.db.has_column(SETTING_DOCTYPE, "authentication_method"):
		return

	if not frappe.db.get_single_value(SETTING_DOCTYPE, "authentication_method"):
		frappe.db.set_single_value(
			SETTING_DOCTYPE, "authentication_method", "Static Token", update_modified=False
		)
		frappe.logger().info(
			"Shopify Setting: Set default authentication method to 'Static Token' for existing installation"
		)

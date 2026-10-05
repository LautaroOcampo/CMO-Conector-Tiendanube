# -*- coding: utf-8 -*-
{
    'name': "TiendaNube Connector",

    'summary': "Connect Odoo with TiendaNube — sync products, stock and orders in real time",

    'description': """
TiendaNube Connector for Odoo
==============================

Connect your Odoo instance with TiendaNube (Nuvemshop), the leading
e-commerce platform in Latin America.

Requires an active TiendaNube partner application and store
(external service). Authorization uses OAuth 2.0; product, stock
and order data are exchanged with TiendaNube servers.

Features:

* Export Odoo products to TiendaNube
* Import products from TiendaNube into Odoo
* Sync images, variants and attributes
* Sync stock — manual or automatic
* Import orders via webhook notifications and scheduled polling
* OAuth 2.0 authentication with automatic token management
* Automatic pause and unpause of listings based on minimum stock rules

Keywords: TiendaNube, Nuvemshop, e-commerce, ecommerce, marketplace,
Latin America, Argentina, Brazil, inventory sync, order automation,
webhook, stock management, product sync, online store
    """,

    'author': "Galarreta",
    'website': "https://galarreta.co",
    'support': "alvaro@galarreta.co",
    'price': 250.00,
    'currency': 'USD',

    'category': 'Sales',
    'version': '19.0.1.0.47',

    'depends': ['base', 'product', 'sale', 'stock', 'mail', 'account'],

    'images': [
        'static/description/main_screenshot.png',
    ],

    'data': [
        'security/tn_security.xml',
        'security/ir.model.access.csv',
        'security/tn_retire_manager.xml',
        'views/tn_config_views.xml',
        'views/tn_oauth_tenant_views.xml',
        'views/tn_publication_views.xml',
        'views/tn_publication_values_wizard_views.xml',
        'views/tn_import_orders_wizard_views.xml',
        'views/tn_sale_order_views.xml',
        'views/tn_webhook_notification_views.xml',
        'views/tn_sync_log_views.xml',
        'views/sale_order_views.xml',
        'views/report_invoice_templates.xml',
        'views/res_config_settings_views.xml',
        'views/tn_create_publication_wizard_views.xml',
        'data/cron_order_sync.xml',
        'data/cron_product_import.xml',
    ],

    'demo': [],

    'installable': True,
    'application': True,
    'auto_install': False,
    'license': 'LGPL-3',
    'post_init_hook': 'post_init_hook',
}
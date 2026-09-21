# -*- coding: utf-8 -*-
from odoo import api, fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    @api.model
    def _connector_argentina_lang(self):
        """Idioma para contactos creados desde conectores (facturas en español argentino)."""
        Lang = self.env['res.lang']
        lang = Lang._lang_get('es_AR')
        if lang:
            return lang.code
        lang = Lang.search([('code', '=', 'es_AR'), ('active', '=', True)], limit=1)
        return lang.code if lang else False

    tn_customer_id = fields.Char(
        string='ID cliente TiendaNube',
        index=True,
        copy=False,
        help='Identificador del cliente en TiendaNube (customer.id del pedido). Único para evitar duplicados.',
    )

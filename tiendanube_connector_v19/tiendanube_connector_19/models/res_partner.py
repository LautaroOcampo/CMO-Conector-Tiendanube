# -*- coding: utf-8 -*-
from odoo import fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    tn_customer_id = fields.Char(
        string='ID cliente TiendaNube',
        index=True,
        copy=False,
        help='Identificador del cliente en TiendaNube (customer.id del pedido). Único para evitar duplicados.',
    )

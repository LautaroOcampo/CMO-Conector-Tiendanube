# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
    """Extensión de sale.order para vincular con órdenes de TiendaNube"""
    _inherit = 'sale.order'

    sale_origin = fields.Selection(
        [
            ('other', 'Otro'),
            ('mercadolibre', 'Mercado Libre'),
            ('tiendanube', 'TiendaNube'),
        ],
        string='Origen',
        default='other',
        help='Indica de dónde proviene la orden de venta (ej. TiendaNube, Mercado Libre).'
    )

    tn_sale_order_id = fields.Many2one(
        'tn.sale.order',
        string='Orden de Venta TiendaNube',
        ondelete='set null',
        help='Orden de venta de TiendaNube relacionada con esta orden de Odoo'
    )

    def action_view_tn_sale_order(self):
        """Acción para ver la orden de venta de TiendaNube relacionada"""
        self.ensure_one()
        if not self.tn_sale_order_id:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Sin orden de TiendaNube'),
                    'message': _('No hay orden de venta de TiendaNube relacionada con esta orden de Odoo.'),
                    'type': 'warning',
                    'sticky': False,
                }
            }
        
        return {
            'name': _('Orden de Venta TiendaNube'),
            'type': 'ir.actions.act_window',
            'res_model': 'tn.sale.order',
            'res_id': self.tn_sale_order_id.id,
            'view_mode': 'form',
            'target': 'current',
        }


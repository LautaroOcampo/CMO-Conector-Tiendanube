# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
import logging

_logger = logging.getLogger(__name__)


class ProductTemplate(models.Model):
    """Extiende product.template para agregar funcionalidad de TiendaNube"""
    _inherit = 'product.template'

    tn_publication_ids = fields.One2many(
        'tn.publication',
        'odoo_product_id',
        string='Publicaciones TiendaNube',
        help='Publicaciones de TiendaNube asociadas a este producto'
    )
    
    tn_publication_count = fields.Integer(
        string='Publicaciones TN',
        compute='_compute_tn_publication_count',
        help='Número de publicaciones en TiendaNube'
    )
    
    has_tn_publication = fields.Boolean(
        string='Tiene Publicación TN',
        compute='_compute_has_tn_publication',
        search='_search_has_tn_publication',
        help='Indica si el producto tiene una publicación activa en TiendaNube'
    )

    @api.depends('tn_publication_ids')
    def _compute_tn_publication_count(self):
        """Calcula el número de publicaciones asociadas"""
        for product in self:
            product.tn_publication_count = len(product.tn_publication_ids)
    
    @api.depends('tn_publication_ids', 'tn_publication_ids.active')
    def _compute_has_tn_publication(self):
        """Calcula si el producto tiene una publicación activa en TiendaNube"""
        for product in self:
            product.has_tn_publication = bool(
                product.tn_publication_ids.filtered(lambda p: p.active)
            )
    
    def _search_has_tn_publication(self, operator, value):
        """Permite buscar productos por si tienen publicación o no"""
        if operator == '=' and value is True:
            # Productos que tienen publicación activa
            publications = self.env['tn.publication'].search([
                ('odoo_product_id', '!=', False),
                ('active', '=', True)
            ])
            return [('id', 'in', publications.mapped('odoo_product_id').ids)]
        elif operator == '=' and value is False:
            # Productos que NO tienen publicación activa
            publications = self.env['tn.publication'].search([
                ('odoo_product_id', '!=', False),
                ('active', '=', True)
            ])
            product_ids_with_pub = publications.mapped('odoo_product_id').ids
            return [('id', 'not in', product_ids_with_pub)]
        elif operator == '!=' and value is True:
            return self._search_has_tn_publication('=', False)
        elif operator == '!=' and value is False:
            return self._search_has_tn_publication('=', True)
        return []

    def action_view_tn_publications(self):
        """Abre las publicaciones de TiendaNube asociadas"""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Publicaciones TiendaNube'),
            'res_model': 'tn.publication',
            'domain': [('odoo_product_id', '=', self.id)],
            'view_mode': 'list,form',
            'target': 'current',
        }
    
    def _sync_price_to_tiendanube(self):
        """Deshabilitado: el precio en TN solo se actualiza desde la publicación."""
        self.ensure_one()
        _logger.info(
            "ℹ️ Sync de precio a TN desde producto Odoo deshabilitado (producto %s).",
            self.id,
        )
        return

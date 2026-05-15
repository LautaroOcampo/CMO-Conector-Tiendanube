# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import ValidationError
import logging

_logger = logging.getLogger(__name__)

class TNSaleOrderLine(models.Model):
    """Líneas de venta de TiendaNube"""
    _name = 'tn.sale.order.line'
    _description = 'Línea de Venta de TiendaNube'
    _order = 'id'

    order_id = fields.Many2one(
        'tn.sale.order',
        string='Venta',
        required=True,
        ondelete='cascade',
        index=True
    )

    name = fields.Char(
        string='Descripción',
        required=True,
        help='Nombre del producto'
    )

    product_id_tn = fields.Integer(
        string='ID Producto TiendaNube',
        help='ID del producto en TiendaNube'
    )

    variant_id_tn = fields.Integer(
        string='ID Variante TiendaNube',
        help='ID de la variante en TiendaNube'
    )

    publication_id = fields.Many2one(
        'tn.publication',
        string='Publicación',
        ondelete='set null',
        help='Publicación de TiendaNube relacionada'
    )

    publication_variant_id = fields.Many2one(
        'tn.publication.variant',
        string='Variante de Publicación',
        ondelete='set null',
        help='Variante específica de la publicación'
    )

    quantity = fields.Float(
        string='Cantidad',
        required=True,
        default=1.0,
        digits=(16, 3)
    )

    price_unit = fields.Float(
        string='Precio Unitario',
        required=True,
        digits=(16, 2)
    )

    price_subtotal = fields.Float(
        string='Subtotal',
        compute='_compute_price_subtotal',
        digits=(16, 2)
    )

    sku = fields.Char(
        string='SKU',
        help='Código SKU del producto'
    )

    @api.depends('quantity', 'price_unit')
    def _compute_price_subtotal(self):
        """Calcula el subtotal de la línea"""
        for line in self:
            line.price_subtotal = line.quantity * line.price_unit

    @api.model
    def create_line_from_webhook(self, order, line_item):
        """
        Crea una línea de venta desde datos del webhook
        
        Args:
            order: tn.sale.order relacionado
            line_item: Dict con datos de la línea desde TiendaNube
        
        Returns:
            tn.sale.order.line: Registro creado
        """
        try:
            # Obtener información básica
            product_id_tn = line_item.get('product_id') or line_item.get('product', {}).get('id')
            variant_id_tn = line_item.get('variant_id') or line_item.get('variant', {}).get('id')
            name = line_item.get('name') or line_item.get('product', {}).get('name') or _('Producto sin nombre')
            quantity = float(line_item.get('quantity', 1) or 1)
            price_unit = float(line_item.get('price', 0) or 0)
            sku = line_item.get('sku') or line_item.get('product', {}).get('sku') or ''

            # Buscar publicación relacionada
            publication_id = False
            publication_variant_id = False

            if product_id_tn:
                # Buscar por ID de producto en TiendaNube
                publication = self.env['tn.publication'].search([
                    ('tn_product_id', '=', product_id_tn),
                    ('company_id', '=', self.env.company.id)
                ], limit=1)

                if publication:
                    publication_id = publication.id

                    # Si hay variante, buscarla
                    if variant_id_tn:
                        variant = self.env['tn.publication.variant'].search([
                            ('publication_id', '=', publication.id),
                            ('tn_variant_id', '=', variant_id_tn)
                        ], limit=1)

                        if variant:
                            publication_variant_id = variant.id

            # Si no se encontró por ID, intentar por SKU
            if not publication_id and sku:
                variant = self.env['tn.publication.variant'].search([
                    ('sku', '=', sku),
                    ('publication_id.company_id', '=', self.env.company.id)
                ], limit=1)

                if variant:
                    publication_variant_id = variant.id
                    publication_id = variant.publication_id.id
                else:
                    # Buscar en publicación principal
                    publication = self.env['tn.publication'].search([
                        ('sku', '=', sku),
                        ('company_id', '=', self.env.company.id)
                    ], limit=1)

                    if publication:
                        publication_id = publication.id

            # Crear línea
            vals = {
                'order_id': order.id,
                'name': name,
                'product_id_tn': product_id_tn,
                'variant_id_tn': variant_id_tn,
                'publication_id': publication_id,
                'publication_variant_id': publication_variant_id,
                'quantity': quantity,
                'price_unit': price_unit,
                'sku': sku,
            }

            line = self.create(vals)
            _logger.info(
                "✅ Línea de venta creada: %s x %s (Publicación: %s)",
                quantity, name, publication_id or 'No encontrada'
            )

            return line

        except Exception as e:
            _logger.exception("❌ Error creando línea de venta desde webhook: %s", e)
            raise


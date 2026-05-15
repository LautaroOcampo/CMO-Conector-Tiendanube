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

    def write(self, vals):
        """Sobrescribir write para sincronizar precio automáticamente cuando cambia en Odoo"""
        result = super(ProductTemplate, self).write(vals)
        
        # Verificar si se modificó el precio (list_price)
        if 'list_price' in vals:
            # Verificar si hay alguna configuración con sincronización automática de precio activada
            configs = self.env['tn.config'].search([
                ('auto_sync_price_on_odoo_change', '=', True)
            ])
            
            if configs:
                # Sincronizar precio para todas las publicaciones relacionadas
                for product in self:
                    try:
                        product._sync_price_to_tiendanube()
                    except Exception as e:
                        _logger.exception("❌ Error sincronizando precio automáticamente para producto %s: %s", product.id, e)
                        # No fallar la operación si falla la sincronización
        
        return result

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
        """
        Sincroniza el precio de este producto a TiendaNube cuando cambia en Odoo.
        Solo sincroniza si hay configuraciones con auto_sync_price_on_odoo_change activado.
        """
        self.ensure_one()
        
        try:
            _logger.info("=" * 80)
            _logger.info("💰 INICIO: Sincronización automática de precio a TiendaNube")
            _logger.info("📦 Producto: %s (ID: %s)", self.name, self.id)
            
            # Verificar si hay alguna configuración con sincronización automática de precio activada
            configs = self.env['tn.config'].search([
                ('auto_sync_price_on_odoo_change', '=', True)
            ])
            
            if not configs:
                _logger.info("ℹ️ Sincronización automática de precio desde Odoo desactivada. No se sincroniza.")
                _logger.info("=" * 80)
                return
            
            # Buscar publicaciones de TiendaNube relacionadas con este producto
            # Solo de configuraciones con sincronización automática activada
            publications = self.env['tn.publication'].search([
                ('odoo_product_id', '=', self.id),
                ('tn_product_id', '!=', False),  # Solo publicaciones ya sincronizadas
                ('active', '=', True),
                ('tn_config_id', 'in', configs.ids),  # Solo publicaciones de configuraciones con sincronización activada
            ])
            
            if not publications:
                _logger.warning("⚠️ No se encontraron publicaciones de TiendaNube para producto %s (ID: %s)", 
                               self.name, self.id)
                _logger.warning("   Verificar que:")
                _logger.warning("   1. El producto tenga una publicación de TiendaNube relacionada")
                _logger.warning("   2. La publicación tenga tn_product_id (ya sincronizada)")
                _logger.warning("   3. La publicación esté activa")
                _logger.info("=" * 80)
                return
            
            _logger.info("📦 Publicaciones encontradas: %d", len(publications))
            
            # Para cada publicación, actualizar el precio de las variantes relacionadas
            for publication in publications:
                try:
                    _logger.info("🔄 Sincronizando precio para publicación: %s (ID TN: %s)", 
                               publication.title, publication.tn_product_id)
                    
                    # Actualizar precio desde Odoo primero
                    # IMPORTANTE: Esto actualiza el precio con el valor ABSOLUTO actual desde Odoo
                    _logger.info("💰 Actualizando precio desde Odoo para publicación %s...", publication.title)
                    _logger.info("   ℹ️ Se actualizará con el valor ABSOLUTO del precio actual en Odoo")
                    publication._sync_stock_and_price_from_odoo()
                    
                    # Verificar que las variantes tengan precio actualizado
                    _logger.info("💰 Precio de variantes después de actualizar desde Odoo:")
                    for variant in publication.variant_ids:
                        if variant.odoo_variant_id:
                            _logger.info("   - Variante %s (TN ID: %s): precio=%s, odoo_variant_id=%s", 
                                       variant.name or 'sin nombre', variant.tn_variant_id, variant.price, variant.odoo_variant_id.id)
                    
                    # Sincronizar solo el precio (no el stock) a TiendaNube
                    sync_service = self.env['tn.sync.service']
                    if publication.tn_product_id:
                        _logger.info("📤 Enviando actualización de precio a TiendaNube (product_id: %s)...", publication.tn_product_id)
                        try:
                            # Sincronizar solo precio, no stock
                            sync_service._update_variants_individually(publication, publication.tn_product_id, sync_stock=False, sync_price=True)
                            _logger.info("✅ Precio sincronizado exitosamente a TiendaNube para publicación %s", publication.title)
                        except Exception as sync_error:
                            _logger.exception("❌ Error al llamar _update_variants_individually: %s", sync_error)
                            raise
                    else:
                        _logger.warning("⚠️ Publicación %s no tiene tn_product_id, no se puede sincronizar", publication.title)
                        
                except Exception as e:
                    _logger.exception("❌ Error sincronizando publicación %s a TiendaNube: %s", publication.title, e)
                    # Continuar con otras publicaciones aunque una falle
                    continue
            
            _logger.info("=" * 80)
            
        except Exception as e:
            _logger.exception("❌ Error en sincronización automática de precio a TiendaNube: %s", e)


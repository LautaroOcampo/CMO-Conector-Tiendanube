# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)


class TNCreatePublicationWizard(models.TransientModel):
    """Wizard para crear publicaciones en TiendaNube desde productos de Odoo"""
    _name = 'tn.create.publication.wizard'
    _description = 'Wizard: Crear Publicaciones TiendaNube'

    tn_config_id = fields.Many2one(
        'tn.config',
        string='Cuenta TiendaNube',
        required=True,
        help='Cuenta de TiendaNube a usar para crear las publicaciones'
    )
    
    product_ids = fields.Many2many(
        'product.template',
        'tn_wizard_product_rel',
        'wizard_id',
        'product_id',
        string='Productos a Publicar',
        domain=[('active', '=', True)],
        help='Seleccione los productos de Odoo que desea publicar en TiendaNube'
    )
    
    @api.model
    def default_get(self, fields_list):
        """Pre-selecciona productos y cuenta si vienen del contexto"""
        res = super(TNCreatePublicationWizard, self).default_get(fields_list)
        
        # Pre-seleccionar cuenta si no está en el contexto
        if 'tn_config_id' not in res and 'tn_config_id' in fields_list:
            config = self.env['tn.config'].get_config()
            if config:
                res['tn_config_id'] = config.id
        
        # Si hay productos seleccionados en el contexto (desde la lista), pre-seleccionarlos
        if 'active_ids' in self.env.context:
            product_ids = self.env.context.get('active_ids', [])
            if product_ids and 'product_ids' in fields_list:
                # Filtrar solo productos activos sin publicación TN
                products = self.env['product.template'].browse(product_ids).filtered(
                    lambda p: p.active and not p.has_tn_publication
                )
                if products:
                    res['product_ids'] = [(6, 0, products.ids)]
        
        return res


    def action_create_publications(self):
        """Crea publicaciones en TiendaNube para los productos seleccionados"""
        self.ensure_one()
        
        if not self.product_ids:
            raise UserError(_('Debe seleccionar al menos un producto para publicar.'))
        
        if not self.tn_config_id:
            raise UserError(_('Debe seleccionar una cuenta de TiendaNube.'))
        
        # Verificar configuración
        config = self.tn_config_id
        if not config.sudo().access_token or not config.store_id:
            raise UserError(_('La cuenta seleccionada debe tener Access Token y Store ID configurados.'))
        
        _logger.info("=" * 80)
        _logger.info("🚀 INICIO: Creación de publicaciones en TiendaNube")
        _logger.info("📦 Productos seleccionados: %d", len(self.product_ids))
        
        created_count = 0
        error_count = 0
        errors = []
        
        sync_service = self.env['tn.sync.service']
        
        for product in self.product_ids:
            try:
                _logger.info("-" * 80)
                _logger.info("📦 Procesando producto: %s (ID: %s)", product.name, product.id)
                
                # Verificar si ya tiene publicación
                existing_publication = self.env['tn.publication'].search([
                    ('odoo_product_id', '=', product.id),
                    ('active', '=', True)
                ], limit=1)
                
                if existing_publication:
                    _logger.warning("⚠️ El producto %s ya tiene una publicación (ID: %s), omitiendo.", 
                                  product.name, existing_publication.id)
                    continue
                
                # Obtener stock
                stock_location = None
                if config.warehouse_id:
                    stock_location = config.warehouse_id.lot_stock_id
                elif config.stock_location_id:
                    stock_location = config.stock_location_id
                
                # Obtener tipo de stock desde la configuración
                stock_type = config.stock_type if config else 'available'
                use_expected = (stock_type == 'expected')
                
                stock_value = 0.0
                if stock_location:
                    if use_expected:
                        # Stock esperado: usar virtual_available que considera entradas esperadas y reservas
                        stock_value = sum(product.product_variant_ids.mapped(lambda v: v.with_context(location=stock_location.id).virtual_available or 0.0))
                    else:
                        # Stock disponible: usar cantidad física
                        quants = self.env['stock.quant'].search([
                            ('product_id', 'in', product.product_variant_ids.ids),
                            ('location_id', 'child_of', stock_location.id),
                        ])
                        stock_value = sum(quants.mapped('quantity'))
                else:
                    # Stock total
                    if use_expected:
                        stock_value = sum(product.product_variant_ids.mapped('virtual_available'))
                    else:
                        stock_value = sum(product.product_variant_ids.mapped('qty_available'))
                
                # Crear publicación en Odoo
                publication_vals = {
                    'title': product.name,
                    'odoo_product_id': product.id,
                    'tn_config_id': config.id,  # Asignar la cuenta seleccionada
                    'sku': product.default_code or '',
                    'published': False,  # Por defecto no publicado hasta que se sincronice
                    'sync_state': 'draft',
                }
                
                # Crear publicación
                publication = self.env['tn.publication'].create(publication_vals)
                _logger.info("✅ Publicación creada en Odoo: %s (ID: %s)", publication.title, publication.id)
                
                # Agregar imagen principal después de crear la publicación
                if product.image_1920:
                    # Eliminar imágenes existentes si las hay
                    publication.image_ids.unlink()
                    
                    # Crear imagen principal
                    self.env['tn.publication.image'].create({
                        'publication_id': publication.id,
                        'image': product.image_1920,
                        'position': 1,
                    })
                    _logger.info("✅ Imagen principal agregada a la publicación")
                
                # Sincronizar variantes desde el producto de Odoo
                # Esto se hace automáticamente en el create/write de tn.publication
                # pero lo hacemos explícitamente aquí para asegurar
                publication._sync_variants_from_odoo_product()
                
                # Actualizar stock desde Odoo (sin copiar list_price)
                publication._sync_stock_and_price_from_odoo()

                # Requisito funcional: al crear desde product.template no enviar códigos de barras.
                # Limpiamos barcode de publicación y de sus variantes antes de exportar.
                publication.write({'barcode': False})
                publication.variant_ids.write({'barcode': False})

                # Requisito funcional: publicado si tiene stock total > 0.
                total_stock = sum(publication.variant_ids.mapped('stock'))
                publication.write({'published': total_stock > 0})
                
                # Exportar a TiendaNube y guardar el ID en la ficha de Odoo
                _logger.info("📤 Exportando publicación a TiendaNube...")
                result = sync_service.export_publication(publication)
                tn_id = (result or {}).get('product_id')
                _logger.info("[TN BIND] wizard result pub=%s result=%s", publication.id, result)
                if tn_id:
                    sync_service._bind_publication_to_tn_product(publication, tn_id)
                publication.invalidate_recordset(['tn_product_id', 'sync_state'])
                _logger.info(
                    "[TN BIND] wizard after bind pub=%s tn_product_id=%s",
                    publication.id, publication.tn_product_id,
                )
                if not publication.tn_product_id:
                    raise UserError(_(
                        'TiendaNube no devolvió el ID del producto para "%s". '
                        'Revise el log de Odoo (líneas [TN BIND] / [TN EXPORT]).'
                    ) % publication.title)
                
                created_count += 1
                _logger.info(
                    "✅ Publicación creada y exportada: %s (tn_product_id=%s)",
                    publication.title,
                    publication.tn_product_id,
                )
                
            except Exception as e:
                error_count += 1
                error_msg = str(e)
                errors.append(f"{product.name}: {error_msg}")
                _logger.exception("❌ Error creando publicación para producto %s: %s", product.name, e)
        
        _logger.info("=" * 80)
        _logger.info("📊 RESUMEN:")
        _logger.info("   - Publicaciones creadas: %d", created_count)
        _logger.info("   - Errores: %d", error_count)
        _logger.info("=" * 80)
        
        # Mostrar mensaje de resultado
        if error_count == 0:
            message = _('Se crearon exitosamente %d publicación(es) en TiendaNube.') % created_count
            message_type = 'success'
        elif created_count > 0:
            message = _('Se crearon %d publicación(es) exitosamente. %d error(es):\n%s') % (
                created_count, error_count, '\n'.join(errors[:5])
            )
            message_type = 'warning'
        else:
            message = _('No se pudo crear ninguna publicación. Errores:\n%s') % '\n'.join(errors[:5])
            message_type = 'danger'
        
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Creación de Publicaciones'),
                'message': message,
                'type': message_type,
                'sticky': error_count > 0,
            }
        }


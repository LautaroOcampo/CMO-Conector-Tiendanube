# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError
import logging
import base64
import requests
from io import BytesIO

_logger = logging.getLogger(__name__)


class TNPublication(models.Model):
    """Modelo para gestionar publicaciones de TiendaNube"""
    _name = 'tn.publication'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _description = 'Publicación TiendaNube'
    _order = 'create_date desc'
    _rec_name = 'title'

    # Campos básicos
    title = fields.Char(string='Título', required=True, help='Título del producto en TiendaNube')
    description = fields.Html(string='Descripción', help='Descripción del producto')
    description_text = fields.Text(string='Descripción (Texto)', help='Descripción en texto plano')
    sku = fields.Char(string='SKU', help='Código SKU del producto (a nivel de producto)')
    barcode = fields.Char(string='Código de Barras', help='Código de barras del producto (GTIN, EAN, ISBN, etc.)')
    
    # Relaciones
    odoo_product_id = fields.Many2one(
        'product.template',
        string='Producto Odoo',
        ondelete='set null',
        help='Producto de Odoo asociado'
    )
    odoo_variant_display_id = fields.Many2one(
        'product.product',
        string='Variante Odoo',
        compute='_compute_odoo_variant_display',
        inverse='_inverse_odoo_variant_display',
        help='Cuando la publicación tiene una sola variante, permite ver o cambiar la variante de Odoo enlazada'
    )
    show_odoo_variant_field = fields.Boolean(
        string='Mostrar campo Variante Odoo',
        compute='_compute_show_odoo_variant_field',
        help='True si el producto de Odoo tiene variantes y la publicación tiene una sola variante (para ocultar el campo cuando no aplica).'
    )

    tn_config_id = fields.Many2one(
        'tn.config',
        string='Cuenta TiendaNube',
        required=True,
        ondelete='restrict',
        help='Cuenta de TiendaNube a usar para esta publicación',
        index=True
    )
    
    # IDs de sincronización
    tn_product_id = fields.Integer(
        string='ID TiendaNube',
        help='ID del producto en TiendaNube',
        index=True
    )
    main_tn_variant_id = fields.Integer(
        string='ID Variante TN (publicación por variante)',
        default=0,
        index=True,
        help='Cuando la publicación representa una variante concreta (importación a nivel variante), ID de la variante en TiendaNube. 0 o vacío si es publicación a nivel producto.'
    )
    
    # Estado de sincronización
    sync_state = fields.Selection([
        ('draft', 'Borrador'),
        ('synced', 'Sincronizado'),
        ('pending', 'Pendiente'),
        ('error', 'Error'),
    ], string='Estado de Sincronización', default='draft', required=True)
    
    sync_error_message = fields.Text(string='Mensaje de Error', help='Último error de sincronización')
    last_sync_date = fields.Datetime(string='Última Sincronización', help='Fecha de última sincronización exitosa')
    
    # Imágenes
    image_ids = fields.One2many(
        'tn.publication.image',
        'publication_id',
        string='Imágenes',
        help='Imágenes del producto'
    )
    
    # Variantes
    variant_ids = fields.One2many(
        'tn.publication.variant',
        'publication_id',
        string='Variantes',
        help='Variantes del producto (talla, color, SKU, etc.)'
    )
    
    # Atributos/Características
    attribute_ids = fields.One2many(
        'tn.publication.attribute',
        'publication_id',
        string='Atributos',
        help='Atributos personalizados del producto'
    )
    
    # Campos adicionales
    handle = fields.Char(
        string='Handle', 
        help='Handle es el identificador único de la URL del producto en TiendaNube (slug). Ejemplo: si la URL es "mi-tienda.com/products/mi-producto", el handle es "mi-producto"'
    )
    published = fields.Boolean(string='Publicado', default=False, help='Si está publicado en TiendaNube')
    tag_ids = fields.Many2many(
        'tn.publication.tag',
        'tn_publication_tag_rel',
        'publication_id',
        'tag_id',
        string='Tags',
        help='Tags del producto'
    )
    marca = fields.Char(string='Marca', help='Marca del producto')
    categoria_ids = fields.Many2many(
        'tn.publication.category',
        'tn_publication_category_rel',
        'publication_id',
        'category_id',
        string='Categorías',
        help='Categorías del producto en TiendaNube'
    )
    
    # Dimensiones físicas (a nivel de producto)
    weight = fields.Float(string='Peso (kg)', help='Peso del producto en kilogramos')
    width = fields.Float(string='Ancho (cm)', help='Ancho del producto en centímetros')
    height = fields.Float(string='Alto (cm)', help='Alto del producto en centímetros')
    depth = fields.Float(string='Profundidad (cm)', help='Profundidad del producto en centímetros')
    
    # Metadatos
    company_id = fields.Many2one(
        'res.company',
        string='Compañía',
        default=lambda self: self.env.company,
        required=True
    )
    
    active = fields.Boolean(string='Activo', default=True)
    
    # Campos computados para mostrar precio y stock actuales en TN vs valores a actualizar
    current_price_tn = fields.Float(
        string='Precio Actual en TiendaNube',
        compute='_compute_current_values_tn',
        help='Precio actual de la publicación en TiendaNube (último valor sincronizado)'
    )
    
    current_stock_tn = fields.Float(
        string='Stock Actual en TiendaNube',
        compute='_compute_current_values_tn',
        help='Stock actual de la publicación en TiendaNube (último valor sincronizado)'
    )
    
    price_to_update = fields.Float(
        string='Precio al Actualizar',
        compute='_compute_values_to_update',
        help='Precio que se actualizará desde Odoo'
    )
    
    stock_to_update = fields.Float(
        string='Stock al Actualizar',
        compute='_compute_values_to_update',
        help='Stock que se actualizará desde Odoo'
    )
    
    # Campo para determinar si la sincronización automática está activa (para ocultar campos)
    has_auto_sync = fields.Boolean(
        string='Tiene Sincronización Automática',
        compute='_compute_has_auto_sync',
        help='Indica si la configuración tiene sincronización automática activada'
    )
    
    # Control de stock mínimo específico por publicación (sobrescribe valores globales)
    enable_min_stock_pause = fields.Boolean(
        string='Activar Pausa Automática por Stock',
        default=False,
        help='Si está activado, esta publicación se pausará automáticamente cuando el stock caiga por debajo del umbral de pausa. Si está desactivado, se usará la configuración global.'
    )
    
    enable_min_stock_unpause = fields.Boolean(
        string='Activar Despausa Automática por Stock',
        default=False,
        help='Si está activado, esta publicación se despausará automáticamente cuando el stock supere el umbral de despausa. Si está desactivado, se usará la configuración global.'
    )
    
    min_stock_pause = fields.Float(
        string='Stock Mínimo para Pausar',
        help='Stock mínimo específico para esta publicación. Cuando el stock total caiga por debajo de este valor, se pausará automáticamente (si la pausa automática está activada). Si está vacío, se usará el valor global de la configuración.'
    )
    
    min_stock_unpause = fields.Float(
        string='Stock Mínimo para Despausar',
        help='Stock mínimo específico para esta publicación. Cuando el stock total supere este valor, se despausará automáticamente (si la despausa automática está activada). Si está vacío, se usará el valor global de la configuración.'
    )
    
    @api.depends('tn_config_id', 'tn_config_id.auto_sync_stock_on_odoo_change', 'tn_config_id.auto_sync_price_on_odoo_change')
    def _compute_has_auto_sync(self):
        """Calcula si la configuración tiene sincronización automática activada"""
        for record in self:
            config = record.tn_config_id
            if config:
                record.has_auto_sync = config.auto_sync_stock_on_odoo_change or config.auto_sync_price_on_odoo_change
            else:
                # Si no hay config, buscar una global
                global_config = self.env['tn.config'].get_config()
                if global_config:
                    record.has_auto_sync = global_config.auto_sync_stock_on_odoo_change or global_config.auto_sync_price_on_odoo_change
                else:
                    record.has_auto_sync = False
    
    @api.depends('variant_ids.price', 'variant_ids.stock', 'has_auto_sync', 'tn_config_id', 
                 'tn_config_id.auto_sync_stock_on_odoo_change', 'tn_config_id.auto_sync_price_on_odoo_change')
    def _compute_current_values_tn(self):
        """
        Calcula los valores actuales en TiendaNube.
        Si la sincronización automática está activada, estos campos no participan en procesos
        y se establecen en 0 para evitar bugs (los valores siempre coinciden con Odoo).
        """
        for record in self:
            # Si la sincronización automática está activada, no calcular estos valores
            # porque siempre coinciden con Odoo y pueden generar confusión/bugs
            if record.has_auto_sync:
                record.current_price_tn = 0.0
                record.current_stock_tn = 0.0
                continue
            
            # Solo calcular si NO hay sincronización automática
            if not record.variant_ids:
                # Sin variantes: usar valores del producto si existen
                record.current_price_tn = 0.0
                record.current_stock_tn = 0.0
            else:
                # Con variantes: mostrar el precio mínimo y la suma del stock
                prices = [v.price for v in record.variant_ids if v.price > 0]
                record.current_price_tn = min(prices) if prices else 0.0
                record.current_stock_tn = sum(record.variant_ids.mapped('stock'))
    
    @api.depends('variant_ids', 'odoo_product_id', 'odoo_product_id.product_variant_ids')
    def _compute_show_odoo_variant_field(self):
        """Mostrar campo Variante Odoo solo si el producto tiene variantes (más de una)."""
        for record in self:
            record.show_odoo_variant_field = (
                len(record.variant_ids) == 1
                and record.odoo_product_id
                and len(record.odoo_product_id.product_variant_ids) > 1
            )

    @api.depends('variant_ids', 'variant_ids.odoo_variant_id')
    def _compute_odoo_variant_display(self):
        """Muestra la variante Odoo cuando la publicación tiene una sola variante (editable)."""
        for record in self:
            if len(record.variant_ids) == 1:
                record.odoo_variant_display_id = record.variant_ids[0].odoo_variant_id
            else:
                record.odoo_variant_display_id = False

    def _inverse_odoo_variant_display(self):
        """Al cambiar la variante desde el formulario, actualiza la variante de la publicación."""
        for record in self:
            if len(record.variant_ids) != 1:
                continue
            new_variant = record.odoo_variant_display_id
            record.variant_ids[0].odoo_variant_id = new_variant
            if new_variant and not record.odoo_product_id:
                record.odoo_product_id = new_variant.product_tmpl_id

    @api.depends('variant_ids', 'variant_ids.odoo_variant_id',
                 'odoo_product_id', 'odoo_product_id.product_variant_ids.qty_available',
                 'odoo_product_id.product_variant_ids.virtual_available', 'odoo_product_id.list_price',
                 'variant_ids.odoo_variant_id.qty_available', 'variant_ids.odoo_variant_id.virtual_available',
                 'variant_ids.odoo_variant_id.list_price')
    def _compute_values_to_update(self):
        """
        Calcula los valores que se actualizarán desde Odoo.
        Si las variantes de la publicación tienen odoo_variant_id, usa solo esas variantes
        (precio y stock por variante enlazada). Si no, usa todas las variantes del producto.
        """
        for record in self:
            if not record.odoo_product_id:
                record.price_to_update = 0.0
                record.stock_to_update = 0.0
                continue

            try:
                config = record.tn_config_id if record.tn_config_id else self.env['tn.config'].get_config()
                stock_type = config.stock_type if config else 'available'
                use_expected = (stock_type == 'expected')
                stock_location = None
                if config and config.warehouse_id:
                    stock_location = config.warehouse_id.lot_stock_id
                elif config and config.stock_location_id:
                    stock_location = config.stock_location_id
                pricelist = config.pricelist_id if config else None

                prices = []
                total_stock = 0.0
                # Usar solo variantes de la publicación que tienen odoo_variant_id enlazado
                odoo_variants_to_use = [v.odoo_variant_id for v in record.variant_ids if v.odoo_variant_id]
                if not odoo_variants_to_use:
                    # Fallback: todas las variantes del producto template
                    odoo_variants_to_use = record.odoo_product_id.product_variant_ids

                for variant in odoo_variants_to_use:
                    price = variant.list_price
                    if pricelist:
                        try:
                            price = pricelist._get_product_price(variant.product_tmpl_id, 1.0)
                            if not price:
                                price = variant.list_price
                        except Exception:
                            price = variant.list_price
                    if price > 0:
                        prices.append(price)

                    if stock_location:
                        if use_expected:
                            stock_value = float(variant.with_context(location=stock_location.id).virtual_available or 0.0)
                        else:
                            StockQuant = self.env['stock.quant']
                            quants = StockQuant.search([
                                ('product_id', '=', variant.id),
                                ('location_id', 'child_of', stock_location.id),
                            ])
                            stock_value = sum(quants.mapped('quantity'))
                    else:
                        if use_expected:
                            stock_value = float(variant.virtual_available or 0.0)
                        else:
                            stock_value = float(variant.qty_available or 0.0)
                    total_stock += stock_value

                if not odoo_variants_to_use:
                    price = record.odoo_product_id.list_price
                    if pricelist:
                        try:
                            price = pricelist._get_product_price(record.odoo_product_id, 1.0)
                            if not price:
                                price = record.odoo_product_id.list_price
                        except Exception:
                            price = record.odoo_product_id.list_price
                    if price > 0:
                        prices.append(price)

                record.price_to_update = min(prices) if prices else 0.0
                record.stock_to_update = total_stock

            except Exception as e:
                _logger.warning("⚠️ Error calculando valores a actualizar para publicación %s: %s", record.id, str(e))
                record.price_to_update = 0.0
                record.stock_to_update = 0.0
    
    @api.model_create_multi
    def create(self, vals_list):
        """Override create para generar handle si no existe y sincronizar variantes"""
        prepared = []
        for vals in vals_list:
            v = dict(vals)
            if not v.get('handle') and v.get('title'):
                v['handle'] = self._generate_handle(v['title'])
            if not v.get('tn_config_id'):
                config = self.env['tn.config'].get_config()
                if config:
                    v['tn_config_id'] = config.id
            prepared.append(v)

        records = super(TNPublication, self).create(prepared)
        for record in records:
            if record.odoo_product_id:
                record._sync_variants_from_odoo_product()
        return records
    
    def write(self, vals):
        """Override write para actualizar handle si cambia el título"""
        if 'title' in vals and not vals.get('handle'):
            vals['handle'] = self._generate_handle(vals['title'])
        
        # Si cambia el producto de Odoo, actualizar variantes automáticamente
        # No hacerlo en publicaciones "por variante" (importadas una fila por variante TN):
        # ahí las variantes vienen de TiendaNube y solo se enlaza por SKU; reemplazarlas
        # por las de Odoo borraría el resto de variantes del producto TN.
        if 'odoo_product_id' in vals:
            record_ids = self.ids
            result = super(TNPublication, self).write(vals)
            records = self.browse(record_ids) if record_ids else self
            for record in records:
                if not record.odoo_product_id:
                    continue
                if record.main_tn_variant_id:
                    continue  # publicación por variante: no reemplazar variantes, ya vienen de TN
                _logger.info("🔄 Actualizando variantes para publicación %s desde producto %s",
                             record.id, record.odoo_product_id.id)
                record._sync_variants_from_odoo_product()
            return result
        
        return super(TNPublication, self).write(vals)
    
    @api.onchange('odoo_product_id')
    def _onchange_odoo_product_id(self):
        """Cuando se selecciona un producto de Odoo, mostrar mensaje informativo"""
        if not self.odoo_product_id:
            return {
                'warning': {
                    'title': _('Información'),
                    'message': _('Las variantes se actualizarán automáticamente al guardar el registro.'),
                }
            }
        return {
            'warning': {
                'title': _('Información'),
                'message': _('Las variantes se actualizarán automáticamente al guardar el registro.'),
            }
        }
    
    def _sync_variants_from_odoo_product(self):
        """Sincroniza las variantes desde el producto de Odoo relacionado"""
        self.ensure_one()
        if not self.odoo_product_id:
            return
        
        product = self.odoo_product_id
        
        # Sincronizar variantes
        if product.product_variant_ids:
            # Eliminar variantes existentes
            self.variant_ids.unlink()
            
            # Crear nuevas variantes basadas en las variantes de Odoo
            for variant in product.product_variant_ids:
                # Obtener atributos de la variante (por ejemplo, Talla, Color, etc.)
                option1_name = option1_value = ''
                option2_name = option2_value = ''
                option3_name = option3_value = ''
                
                # Tomar hasta 3 atributos de la variante de Odoo
                attr_values = variant.product_template_attribute_value_ids
                if attr_values:
                    attrs = list(attr_values)[:3]
                    if len(attrs) >= 1:
                        option1_name = attrs[0].attribute_id.name or ''
                        option1_value = attrs[0].name or ''
                    if len(attrs) >= 2:
                        option2_name = attrs[1].attribute_id.name or ''
                        option2_value = attrs[1].name or ''
                    if len(attrs) >= 3:
                        option3_name = attrs[2].attribute_id.name or ''
                        option3_value = attrs[2].name or ''
                
                # Construir nombre legible de la variante para Odoo
                name_parts = []
                if option1_name and option1_value:
                    name_parts.append(f"{option1_name}: {option1_value}")
                if option2_name and option2_value:
                    name_parts.append(f"{option2_name}: {option2_value}")
                if option3_name and option3_value:
                    name_parts.append(f"{option3_name}: {option3_value}")
                variant_name = ' / '.join(name_parts) if name_parts else (variant.display_name or variant.name or 'Variante')
                
                # Dimensiones desde la variante de Odoo (si tiene) o de la primera variante del template
                first_variant = product.product_variant_ids[0] if product.product_variant_ids else None
                fallback_weight = getattr(first_variant, 'weight', None) or 0.0 if first_variant else 0.0
                fallback_width = getattr(first_variant, 'width', None) or 0.0 if first_variant else 0.0
                fallback_height = getattr(first_variant, 'height', None) or 0.0 if first_variant else 0.0
                fallback_depth = getattr(first_variant, 'length', None) or 0.0 if first_variant else 0.0
                fallback_barcode = getattr(first_variant, 'barcode', None) or '' if first_variant else ''
                
                # Usar getattr también para la variante actual
                variant_weight = getattr(variant, 'weight', None) or fallback_weight
                variant_width = getattr(variant, 'width', None) or fallback_width
                variant_height = getattr(variant, 'height', None) or fallback_height
                variant_depth = getattr(variant, 'length', None) or fallback_depth
                variant_barcode = getattr(variant, 'barcode', None) or fallback_barcode
                
                # Calcular stock según el tipo configurado
                config = self.tn_config_id if self.tn_config_id else self.env['tn.config'].get_config()
                stock_type = config.stock_type if config else 'available'
                use_expected = (stock_type == 'expected')
                
                if use_expected:
                    variant_stock = variant.virtual_available or 0.0
                else:
                    variant_stock = variant.qty_available or 0.0
                
                self.env['tn.publication.variant'].create({
                    'publication_id': self.id,
                    'name': variant_name,
                    'sku': variant.default_code or '',
                    'price': variant.list_price,
                    'stock': variant_stock,
                    'weight': variant_weight,
                    'width': variant_width,
                    'height': variant_height,
                    'depth': variant_depth,
                    'barcode': variant_barcode,
                    'odoo_variant_id': variant.id,
                    'option1_name': option1_name,
                    'option1_value': option1_value,
                    'option2_name': option2_name,
                    'option2_value': option2_value,
                    'option3_name': option3_name,
                    'option3_value': option3_value,
                })
        else:
            # Si no hay variantes, eliminar todas las existentes
            self.variant_ids.unlink()
    
    def _generate_handle(self, title):
        """Genera un handle único basado en el título"""
        if not title:
            return ''
        # Convertir a minúsculas y reemplazar espacios y caracteres especiales
        handle = title.lower().strip()
        handle = handle.replace(' ', '-')
        # Remover caracteres especiales
        import re
        handle = re.sub(r'[^a-z0-9\-]', '', handle)
        # Asegurar unicidad
        base_handle = handle
        counter = 1
        while self.search([('handle', '=', handle)], limit=1):
            handle = f"{base_handle}-{counter}"
            counter += 1
        return handle
    
    @api.model
    def action_import_from_tn(self, tn_config=None):
        """Acción para importar productos desde TiendaNube
        
        Args:
            tn_config: Registro de tn.config a usar. Si no se proporciona, se usa la primera configuración disponible.
        """
        try:
            # Si no se proporciona una cuenta, usar la primera disponible
            if not tn_config:
                tn_config = self.env['tn.config'].get_config()
            
            if not tn_config:
                raise UserError(_('No hay configuración de TiendaNube. Configure una cuenta primero.'))
            
            sync_service = self.env['tn.sync.service']
            result = sync_service.import_publications(limit=50, tn_config=tn_config)
            
            count = result.get('count', 0)
            
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Importación completada'),
                    'message': _('Se importaron %d productos desde TiendaNube') % count,
                    'type': 'success',
                    'sticky': False,
                }
            }
        except Exception as e:
            _logger.exception("Error al importar desde TiendaNube: %s", e)
            raise UserError(_('Error al importar productos: %s') % str(e))
    
    def action_sync_stock_and_price_to_tiendanube(self, sync_stock=True, sync_price=True):
        """
        Acción manual para sincronizar el stock y precio de esta publicación a TiendaNube.
        Actualiza el stock y precio desde Odoo y lo sincroniza con TiendaNube.
        
        Args:
            sync_stock: Si True, sincroniza el stock (default: True)
            sync_price: Si True, sincroniza el precio (default: True)
        """
        self.ensure_one()
        
        if not self.tn_product_id:
            raise UserError(_('Esta publicación no está sincronizada con TiendaNube. Exporte primero la publicación.'))
        
        if not self.odoo_product_id:
            raise UserError(_('Esta publicación no tiene un producto de Odoo relacionado.'))
        
        try:
            _logger.info("=" * 80)
            sync_summary = []
            if sync_stock:
                sync_summary.append("stock")
            if sync_price:
                sync_summary.append("precio")
            sync_label = " y ".join(sync_summary) if len(sync_summary) == 2 else (sync_summary[0] if sync_summary else "datos")

            _logger.info("🔄 INICIO: Sincronización manual de %s a TiendaNube", sync_label)
            _logger.info("📦 Publicación: %s (ID TN: %s)", self.title, self.tn_product_id)
            _logger.info("📦 Producto Odoo: %s (ID: %s)", self.odoo_product_id.name, self.odoo_product_id.id)
            
            # Obtener configuración
            config = self.tn_config_id if self.tn_config_id else self.env['tn.config'].get_config()
            if not config:
                raise UserError(_('No hay configuración de TiendaNube. Configure la cuenta en la publicación.'))
            
            # Obtener la ubicación de stock del almacén configurado
            stock_location = None
            if config.warehouse_id:
                stock_location = config.warehouse_id.lot_stock_id
                _logger.info("🏭 Usando almacén configurado: %s (ID: %s)", config.warehouse_id.name, config.warehouse_id.id)
                _logger.info("📍 Ubicación de stock del almacén: %s (ID: %s)", stock_location.name if stock_location else 'N/A', stock_location.id if stock_location else 'N/A')
            elif config.stock_location_id:
                stock_location = config.stock_location_id
                _logger.info("📍 Usando ubicación de stock configurada directamente: %s (ID: %s)", stock_location.name, stock_location.id)
            
            _logger.info("📍 Ubicación de stock final: %s", stock_location.name if stock_location else 'Ninguno (usará stock total)')
            
            # Verificar stock y precio ANTES de actualizar
            _logger.info("📊 Valores ANTES de actualizar desde Odoo:")
            for variant in self.variant_ids:
                if variant.odoo_variant_id:
                    _logger.info("   - Variante %s: stock actual=%s, precio actual=%s", 
                               variant.name or 'sin nombre', variant.stock, variant.price)
            
            # Actualizar stock y precio desde Odoo
            _logger.info("📊 Actualizando valores desde Odoo...")
            self._sync_stock_and_price_from_odoo()
            
            # Forzar guardado de las variantes para asegurar que el stock y precio se guarden
            self.variant_ids.flush_recordset(['stock', 'price'])
            
            # Invalidar caché y leer valores actualizados
            self.variant_ids.invalidate_recordset(['stock', 'price'])
            
            # Si la publicación ya está en TN pero las variantes no tienen tn_variant_id,
            # intentar obtener los IDs desde la API (mapeo por SKU o posición)
            sync_service = self.env['tn.sync.service']
            variants_with_tn_id_any = any(v.tn_variant_id for v in self.variant_ids)
            if self.variant_ids and not variants_with_tn_id_any:
                _logger.info("📥 Variantes sin ID de TiendaNube: intentando obtener desde API...")
                sync_service._refresh_variant_ids_from_api(self, self.tn_product_id, tn_config=config)
                self.variant_ids.invalidate_recordset(['tn_variant_id'])
            
            # Verificar si hay variantes con ID de TiendaNube o es un producto simple
            variants_with_data = []
            variants_with_tn_id = []
            
            # Primero, recopilar todas las variantes con datos
            for variant in self.variant_ids:
                if variant.odoo_variant_id:
                    # Leer el stock y precio directamente desde la variante (ya está actualizado)
                    stock_value = variant.stock or 0.0
                    price_value = variant.price or 0.0
                    _logger.info("   - Variante %s (TN ID: %s, Odoo ID: %s): stock=%s, precio=%s", 
                               variant.name or 'sin nombre', variant.tn_variant_id or 'N/A', variant.odoo_variant_id.id, stock_value, price_value)
                    variants_with_data.append({
                        'name': variant.name or 'sin nombre',
                        'tn_id': variant.tn_variant_id,
                        'stock': stock_value,
                        'price': price_value
                    })
                    # Si tiene tn_variant_id, agregarlo a la lista
                    if variant.tn_variant_id:
                        variants_with_tn_id.append({
                            'name': variant.name or 'sin nombre',
                            'tn_id': variant.tn_variant_id,
                            'stock': stock_value,
                            'price': price_value
                        })
            
            # Determinar si es producto con variantes o sin variantes
            has_variants_with_tn_id = bool(variants_with_tn_id)
            
            if not variants_with_data:
                raise UserError(_('No se encontraron variantes con producto de Odoo relacionado para sincronizar.'))
            
            if has_variants_with_tn_id:
                # Hay variantes con ID de TiendaNube - usar esas
                _logger.info("📦 Producto con variantes en TiendaNube - se actualizarán %d variante(s)", len(variants_with_tn_id))
                variants_with_data = variants_with_tn_id  # Usar solo las que tienen TN ID
            else:
                # No hay variantes con TN ID - tratar como producto simple (variante virtual)
                _logger.info("📦 Producto sin variantes en TiendaNube (o variantes sin ID) - se actualizará la variante virtual")
                # El método _update_variants_individually ya maneja este caso
            
            # Sincronizar con TiendaNube (sync_service ya definido más arriba)
            _logger.info("=" * 80)
            _logger.info("📤 ENVIANDO ACTUALIZACIÓN A TIENDANUBE")
            _logger.info("   Product ID en TiendaNube: %s", self.tn_product_id)
            if has_variants_with_tn_id:
                _logger.info("   Variantes a sincronizar: %d", len(variants_with_data))
                _logger.info("   Estructura de datos que se enviará:")
                for v in variants_with_data:
                    _logger.info("      - Variante '%s' (TN ID: %s): stock=%s, precio=%s", 
                               v['name'], v['tn_id'], v['stock'], v['price'])
            else:
                _logger.info("   Producto sin variantes en TiendaNube - se actualizará la variante virtual")
            _logger.info("=" * 80)
            
            sync_service._update_variants_individually(self, self.tn_product_id, sync_stock=sync_stock, sync_price=sync_price)
            
            _logger.info("=" * 80)
            _logger.info("✅ %s sincronizado(s) exitosamente a TiendaNube para publicación %s", ", ".join(sync_summary), self.title)
            _logger.info("=" * 80)
            
            # En sincronización manual de stock/precio mantenemos flujo unidireccional:
            # Odoo -> TiendaNube (sin traer estado desde TN en esta acción).
            
            # Verificar y actualizar estado de publicación según stock mínimo
            if sync_stock:
                _logger.info("🔍 Llamando a _check_and_update_published_status después de sincronizar stock...")
                try:
                    self._check_and_update_published_status()
                except Exception as e:
                    _logger.exception("⚠️ Error verificando estado de publicación según stock mínimo: %s", e)
                    # Continuar aunque falle la verificación
            
            if has_variants_with_tn_id:
                message = _('Se sincronizó %s correctamente a TiendaNube para %d variante(s).') % (sync_label, len(variants_with_data))
            else:
                message = _('Se sincronizó %s correctamente a TiendaNube para el producto.') % sync_label
            
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Sincronización completada'),
                    'message': message,
                    'type': 'success',
                    'sticky': False,
                }
            }
        except Exception as e:
            _logger.exception("❌ Error sincronizando datos a TiendaNube: %s", e)
            raise UserError(_('Error al sincronizar datos: %s') % str(e))

    def action_sync_stock_to_tiendanube(self):
        """Acción manual para sincronizar solo stock de esta publicación."""
        return self.action_sync_stock_and_price_to_tiendanube(sync_stock=True, sync_price=False)

    def action_sync_price_to_tiendanube(self):
        """Acción manual para sincronizar solo precio de esta publicación."""
        return self.action_sync_stock_and_price_to_tiendanube(sync_stock=False, sync_price=True)
    
    @api.model
    def action_sync_all_stocks_and_prices_to_tiendanube(self):
        """Compatibilidad: sincroniza stock y precio de todas las publicaciones."""
        return self._action_sync_all_to_tiendanube(sync_stock=True, sync_price=True)

    @api.model
    def action_sync_all_stocks_to_tiendanube(self):
        """Sincroniza solo stock de todas las publicaciones."""
        return self._action_sync_all_to_tiendanube(sync_stock=True, sync_price=False)

    @api.model
    def action_sync_all_prices_to_tiendanube(self):
        """Sincroniza solo precio de todas las publicaciones."""
        return self._action_sync_all_to_tiendanube(sync_stock=False, sync_price=True)

    @api.model
    def _action_sync_all_to_tiendanube(self, sync_stock=True, sync_price=True):
        """
        Acción para sincronizar datos de todas las publicaciones activas a TiendaNube.
        Usa la cuenta configurada en cada publicación.
        """
        try:
            sync_summary = []
            if sync_stock:
                sync_summary.append("stock")
            if sync_price:
                sync_summary.append("precio")
            sync_label = " y ".join(sync_summary) if len(sync_summary) == 2 else (sync_summary[0] if sync_summary else "datos")

            _logger.info("=" * 80)
            _logger.info("🔄 INICIO: Sincronización manual de %s de todas las publicaciones", sync_label)
            
            # Buscar publicaciones activas que tengan productos de Odoo relacionados
            publications = self.search([
                ('odoo_product_id', '!=', False),
                ('tn_product_id', '!=', False),  # Solo publicaciones ya sincronizadas
                ('active', '=', True),
                ('tn_config_id', '!=', False),  # Solo publicaciones con cuenta configurada
            ])
            
            if not publications:
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': _('Sin publicaciones'),
                        'message': _('No se encontraron publicaciones para sincronizar.'),
                        'type': 'warning',
                        'sticky': False,
                    }
                }
            
            _logger.info("📦 Publicaciones encontradas para sincronizar: %d", len(publications))
            
            sync_service = self.env['tn.sync.service']
            synced_count = 0
            error_count = 0
            
            for publication in publications:
                try:
                    _logger.info("🔄 Sincronizando %s para publicación: %s (ID TN: %s)",
                               sync_label,
                               publication.title, publication.tn_product_id)
                    
                    # Actualizar stock y precio desde Odoo
                    publication._sync_stock_and_price_from_odoo()
                    
                    # Forzar guardado de las variantes para asegurar que el stock y precio se guarden
                    publication.variant_ids.flush_recordset(['stock', 'price'])
                    
                    # Invalidar caché y leer valores actualizados
                    publication.variant_ids.invalidate_recordset(['stock', 'price'])
                    
                    # Sincronizar con TiendaNube
                    if publication.tn_product_id:
                        sync_service._update_variants_individually(
                            publication,
                            publication.tn_product_id,
                            sync_stock=sync_stock,
                            sync_price=sync_price,
                        )
                        synced_count += 1
                        _logger.info("✅ %s sincronizado(s) para publicación: %s", ", ".join(sync_summary), publication.title)
                        # En sincronización masiva mantenemos flujo unidireccional:
                        # Odoo -> TiendaNube (sin traer estado desde TN en esta acción).
                        
                        # Verificar y actualizar estado de publicación según stock mínimo
                        if sync_stock:
                            _logger.info("🔍 Llamando a _check_and_update_published_status para publicación '%s'...", publication.title)
                            try:
                                publication._check_and_update_published_status()
                            except Exception as e:
                                _logger.exception("⚠️ Error verificando estado de publicación según stock mínimo: %s", e)
                                # Continuar aunque falle la verificación
                    else:
                        _logger.warning("⚠️ Publicación %s no tiene tn_product_id", publication.title)
                        
                except Exception as e:
                    error_count += 1
                    _logger.exception("❌ Error sincronizando publicación %s: %s", publication.title, e)
                    continue
            
            _logger.info("✅ Sincronización completada: %d exitosas, %d errores", synced_count, error_count)
            _logger.info("=" * 80)
            
            message = _('Sincronización de %s: %d exitosas, %d errores') % (sync_label, synced_count, error_count)
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Sincronización completada'),
                    'message': message,
                    'type': 'success' if error_count == 0 else 'warning',
                    'sticky': False,
                }
            }
            
        except Exception as e:
            _logger.exception("❌ Error en sincronización manual de datos: %s", e)
            raise UserError(_('Error al sincronizar datos: %s') % str(e))
    
    def action_auto_link_products_by_sku(self):
        """
        Auto-completa el producto relacionado de Odoo con las publicaciones de TiendaNube
        basándose en el SKU de la variante de la publicación y el default_code del producto de Odoo.
        """
        self.ensure_one()
        try:
            _logger.info("=" * 80)
            _logger.info("🔗 INICIO: Auto-relación de productos por SKU")
            _logger.info("📦 Publicación: %s (ID: %s)", self.title, self.id)
            
            linked_variants = 0
            linked_products = 0
            skipped_variants = 0
            not_found_variants = 0
            
            # Si la publicación no tiene variantes o ya tiene producto relacionado, usar el SKU de la publicación
            if not self.variant_ids or len(self.variant_ids) == 0:
                if not self.sku or not self.sku.strip():
                    _logger.info("⏭️  Publicación sin variantes y sin SKU, omitiendo")
                    skipped_variants += 1
                elif self.odoo_product_id:
                    _logger.info("ℹ️  Publicación ya tiene producto relacionado: %s", self.odoo_product_id.name)
                else:
                    sku = self.sku.strip()
                    _logger.info("🔍 Publicación sin variantes, buscando producto template Odoo con default_code='%s'", sku)
                    
                    # Buscar primero en product.template por default_code
                    odoo_template = self.env['product.template'].search([
                        ('default_code', '=', sku),
                        ('active', '=', True)
                    ], limit=1)
                    
                    if odoo_template:
                        _logger.info("✅ Encontrado producto template: %s (ID: %s)", odoo_template.name, odoo_template.id)
                        self.write({'odoo_product_id': odoo_template.id})
                        linked_products += 1
                        _logger.info("✅ Producto template relacionado: %s (ID: %s)", odoo_template.name, odoo_template.id)
                    else:
                        _logger.warning("⚠️  No se encontró producto template Odoo con default_code='%s'", sku)
                        not_found_variants += 1
            else:
                # Recorrer todas las variantes de la publicación
                for variant in self.variant_ids:
                    if not variant.sku or not variant.sku.strip():
                        _logger.info("⏭️  Variante '%s' no tiene SKU, omitiendo", variant.name or 'sin nombre')
                        skipped_variants += 1
                        continue
                    
                    # Si ya tiene una variante relacionada, omitir
                    if variant.odoo_variant_id:
                        _logger.info("ℹ️  Variante '%s' ya tiene producto relacionado: %s", 
                                   variant.name or 'sin nombre', variant.odoo_variant_id.name)
                        continue
                    
                    sku = variant.sku.strip()
                    _logger.info("🔍 Buscando producto template Odoo con default_code='%s' para variante '%s'", 
                               sku, variant.name or 'sin nombre')
                    
                    # Buscar primero en product.template por default_code
                    odoo_template = self.env['product.template'].search([
                        ('default_code', '=', sku),
                        ('active', '=', True)
                    ], limit=1)
                    
                    odoo_variant = None
                    
                    if odoo_template:
                        # Si encontramos el template, obtener su variante principal
                        odoo_variant = odoo_template.product_variant_id
                        _logger.info("✅ Encontrado producto template: %s (ID: %s), variante: %s (ID: %s)", 
                                   odoo_template.name, odoo_template.id, odoo_variant.name if odoo_variant else 'N/A', odoo_variant.id if odoo_variant else 'N/A')
                    else:
                        # Si no encontramos en template, buscar en product.product (variantes)
                        _logger.info("🔍 No se encontró en product.template, buscando en product.product...")
                        odoo_variant = self.env['product.product'].search([
                            ('default_code', '=', sku),
                            ('active', '=', True)
                        ], limit=1)
                        if odoo_variant:
                            odoo_template = odoo_variant.product_tmpl_id
                            _logger.info("✅ Encontrado en product.product: variante %s (ID: %s), template: %s (ID: %s)", 
                                       odoo_variant.name, odoo_variant.id, odoo_template.name, odoo_template.id)
                    
                    if not odoo_variant:
                        _logger.warning("⚠️  No se encontró producto Odoo con default_code='%s'", sku)
                        not_found_variants += 1
                        continue
                    
                    # Relacionar la variante
                    variant.write({'odoo_variant_id': odoo_variant.id})
                    linked_variants += 1
                    _logger.info("✅ Variante '%s' relacionada con producto Odoo: %s (ID: %s)", 
                               variant.name or 'sin nombre', odoo_variant.name, odoo_variant.id)
                    
                    # Si la publicación no tiene producto relacionado, relacionar el producto template
                    if not self.odoo_product_id and odoo_template:
                        self.write({'odoo_product_id': odoo_template.id})
                        linked_products += 1
                        _logger.info("✅ Producto template relacionado: %s (ID: %s)", 
                                   odoo_template.name, odoo_template.id)
            
            _logger.info("=" * 80)
            _logger.info("📊 RESUMEN:")
            _logger.info("   - Variantes relacionadas: %d", linked_variants)
            _logger.info("   - Productos template relacionados: %d", linked_products)
            _logger.info("   - Variantes sin SKU (omitidas): %d", skipped_variants)
            _logger.info("   - Variantes no encontradas: %d", not_found_variants)
            _logger.info("=" * 80)
            
            message = _('Relaciones completadas: %d variantes, %d productos template. %d no encontradas.') % (
                linked_variants, linked_products, not_found_variants
            )
            
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Auto-relación Completada'),
                    'message': message,
                    'type': 'success' if linked_variants > 0 else 'warning',
                    'sticky': False,
                }
            }
            
        except Exception as e:
            _logger.exception("❌ Error en auto-relación de productos por SKU: %s", e)
            raise UserError(_('Error al auto-relacionar productos: %s') % str(e))
    
    @api.model
    def action_auto_link_all_products_by_sku(self):
        """
        Auto-completa el producto relacionado de Odoo para todas las publicaciones activas
        basándose en el SKU de las variantes y el default_code del producto de Odoo.
        """
        try:
            _logger.info("=" * 80)
            _logger.info("🔗 INICIO: Auto-relación global de productos por SKU")
            
            # Buscar todas las publicaciones activas
            publications = self.search([('active', '=', True)])
            
            if not publications:
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': _('Sin publicaciones'),
                        'message': _('No se encontraron publicaciones para procesar.'),
                        'type': 'warning',
                        'sticky': False,
                    }
                }
            
            _logger.info("📦 Publicaciones encontradas: %d", len(publications))
            
            total_linked_variants = 0
            total_linked_products = 0
            total_skipped_variants = 0
            total_not_found_variants = 0
            
            for publication in publications:
                try:
                    _logger.info("-" * 80)
                    _logger.info("📦 Procesando publicación: %s (ID: %s)", publication.title, publication.id)
                    
                    linked_variants = 0
                    linked_products = 0
                    skipped_variants = 0
                    not_found_variants = 0
                    
                    # Si la publicación no tiene variantes, usar el SKU de la publicación
                    if not publication.variant_ids or len(publication.variant_ids) == 0:
                        if not publication.sku or not publication.sku.strip():
                            skipped_variants += 1
                        elif publication.odoo_product_id:
                            # Ya tiene producto relacionado, omitir
                            pass
                        else:
                            sku = publication.sku.strip()
                            
                            # Buscar primero en product.template por default_code
                            odoo_template = self.env['product.template'].search([
                                ('default_code', '=', sku),
                                ('active', '=', True)
                            ], limit=1)
                            
                            if odoo_template:
                                publication.write({'odoo_product_id': odoo_template.id})
                                linked_products += 1
                            else:
                                not_found_variants += 1
                    else:
                        # Recorrer todas las variantes de la publicación
                        for variant in publication.variant_ids:
                            if not variant.sku or not variant.sku.strip():
                                skipped_variants += 1
                                continue
                            
                            # Si ya tiene una variante relacionada, omitir
                            if variant.odoo_variant_id:
                                continue
                            
                            sku = variant.sku.strip()
                            
                            # Buscar primero en product.template por default_code
                            odoo_template = self.env['product.template'].search([
                                ('default_code', '=', sku),
                                ('active', '=', True)
                            ], limit=1)
                            
                            odoo_variant = None
                            
                            if odoo_template:
                                # Si encontramos el template, obtener su variante principal
                                odoo_variant = odoo_template.product_variant_id
                            else:
                                # Si no encontramos en template, buscar en product.product (variantes)
                                odoo_variant = self.env['product.product'].search([
                                    ('default_code', '=', sku),
                                    ('active', '=', True)
                                ], limit=1)
                                if odoo_variant:
                                    odoo_template = odoo_variant.product_tmpl_id
                            
                            if not odoo_variant:
                                not_found_variants += 1
                                continue
                            
                            # Relacionar la variante
                            variant.write({'odoo_variant_id': odoo_variant.id})
                            linked_variants += 1
                            
                            # Si la publicación no tiene producto relacionado, relacionar el producto template
                            if not publication.odoo_product_id and odoo_template:
                                publication.write({'odoo_product_id': odoo_template.id})
                                linked_products += 1
                    
                    total_linked_variants += linked_variants
                    total_linked_products += linked_products
                    total_skipped_variants += skipped_variants
                    total_not_found_variants += not_found_variants
                    
                    if linked_variants > 0 or linked_products > 0:
                        _logger.info("✅ Publicación '%s': %d variantes, %d productos relacionados", 
                                   publication.title, linked_variants, linked_products)
                    
                except Exception as e:
                    _logger.exception("❌ Error procesando publicación %s: %s", publication.title, e)
                    continue
            
            _logger.info("=" * 80)
            _logger.info("📊 RESUMEN GLOBAL:")
            _logger.info("   - Variantes relacionadas: %d", total_linked_variants)
            _logger.info("   - Productos template relacionados: %d", total_linked_products)
            _logger.info("   - Variantes sin SKU (omitidas): %d", total_skipped_variants)
            _logger.info("   - Variantes no encontradas: %d", total_not_found_variants)
            _logger.info("=" * 80)
            
            message = _('Relaciones completadas: %d variantes, %d productos template. %d no encontradas.') % (
                total_linked_variants, total_linked_products, total_not_found_variants
            )
            
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Auto-relación Global Completada'),
                    'message': message,
                    'type': 'success' if total_linked_variants > 0 else 'warning',
                    'sticky': False,
                }
            }
            
        except Exception as e:
            _logger.exception("❌ Error en auto-relación global de productos por SKU: %s", e)
            raise UserError(_('Error al auto-relacionar productos: %s') % str(e))
    
    @api.model
    def cron_sync_stock_to_tiendanube(self):
        """
        Método cron para sincronizar stock de publicaciones con TiendaNube.
        Se ejecuta cada 5 minutos para asegurar que el stock esté actualizado.
        """
        try:
            _logger.info("=" * 80)
            _logger.info("🔄 INICIO: Sincronización periódica de stock con TiendaNube (Cron)")
            
            # Obtener configuración
            config = self.tn_config_id if self.tn_config_id else self.env['tn.config'].get_config()
            if not config or not config.stock_location_id:
                _logger.info("ℹ️ No hay configuración de TiendaNube o almacén configurado. No se sincroniza.")
                _logger.info("=" * 80)
                return
            
            # Buscar publicaciones activas que tengan productos de Odoo relacionados
            publications = self.search([
                ('odoo_product_id', '!=', False),
                ('tn_product_id', '!=', False),  # Solo publicaciones ya sincronizadas
                ('active', '=', True),
            ], limit=50)  # Limitar a 50 por ejecución para no sobrecargar
            
            if not publications:
                _logger.info("ℹ️ No se encontraron publicaciones para sincronizar")
                _logger.info("=" * 80)
                return
            
            _logger.info("📦 Publicaciones encontradas para sincronizar: %d", len(publications))
            
            sync_service = self.env['tn.sync.service']
            synced_count = 0
            error_count = 0
            
            for publication in publications:
                try:
                    # Actualizar stock desde Odoo
                    publication._sync_stock_and_price_from_odoo()
                    
                    # Sincronizar con TiendaNube
                    if publication.tn_product_id:
                        sync_service._update_variants_individually(publication, publication.tn_product_id)
                        synced_count += 1
                        _logger.info("✅ Stock sincronizado para publicación: %s", publication.title)
                        
                        # Verificar y actualizar estado de publicación según stock mínimo
                        _logger.info("🔍 Llamando a _check_and_update_published_status para publicación '%s'...", publication.title)
                        try:
                            publication._check_and_update_published_status()
                        except Exception as e:
                            _logger.exception("⚠️ Error verificando estado de publicación según stock mínimo: %s", e)
                            # Continuar aunque falle la verificación
                    else:
                        _logger.warning("⚠️ Publicación %s no tiene tn_product_id", publication.title)
                        
                except Exception as e:
                    error_count += 1
                    _logger.exception("❌ Error sincronizando publicación %s: %s", publication.title, e)
                    continue
            
            _logger.info("✅ Sincronización completada: %d exitosas, %d errores", synced_count, error_count)
            _logger.info("=" * 80)
            
        except Exception as e:
            _logger.exception("❌ Error en sincronización periódica de stock: %s", e)
    
    def action_export_to_tn(self):
        """Exporta/Publica la publicación en TiendaNube"""
        self.ensure_one()
        try:
            sync_service = self.env['tn.sync.service']
            sync_service.export_publication(self)
            
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Exportación completada'),
                    'message': _('La publicación se ha exportado correctamente a TiendaNube'),
                    'type': 'success',
                    'sticky': False,
                }
            }
        except Exception as e:
            _logger.exception("Error al exportar publicación a TiendaNube: %s", e)
            raise UserError(_('Error al exportar publicación: %s') % str(e))
    
    def action_pause_on_tn(self):
        """
        Pausa la publicación en TiendaNube (published=False).

        Importante:
        - En TiendaNube el estado publicado es a nivel PRODUCTO, no por variante.
        - Si hay varias publicaciones en Odoo para el mismo tn_product_id (una por
          variante), pausar una debe pausar TODAS a nivel de producto y sincronizar
          el flag sin cambiar nombre ni atributos.
        """
        if not self:
            return False

        # Agrupar por (config, tn_product_id) para hacer una sola llamada a la API por producto
        sync_service = self.env['tn.sync.service']
        grouped = {}
        for rec in self:
            if not rec.tn_product_id:
                raise UserError(_('La publicación no está sincronizada con TiendaNube. Exporte primero la publicación.'))
            key = (rec.tn_config_id.id or 0, rec.tn_product_id)
            grouped.setdefault(key, self.env['tn.publication'])
            grouped[key] |= rec

        for (config_id, tn_product_id), pubs in grouped.items():
            # Marcar como no publicado TODAS las publicaciones locales de ese producto
            siblings = self.search([('tn_product_id', '=', tn_product_id)])
            siblings.write({'published': False})

            # Obtener configuración para la llamada a la API
            tn_config = pubs[0].tn_config_id if pubs and pubs[0].tn_config_id else None
            try:
                sync_service._set_product_published(tn_product_id, False, tn_config=tn_config)
            except Exception as e:
                _logger.exception("Error al pausar publicación en TiendaNube: %s", e)
                raise UserError(_('Error al pausar en TiendaNube: %s') % str(e))

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Pausada'),
                'message': _('La publicación se ha pausado en TiendaNube'),
                'type': 'success',
                'sticky': False,
            }
        }
    
    def action_activate_on_tn(self):
        """
        Activa la publicación en TiendaNube (published=True).

        Igual que en pause: el cambio es a nivel PRODUCTO. Activar una variante
        debe activar el producto completo en TiendaNube y reflejarlo en todas las
        publicaciones locales que compartan tn_product_id.
        """
        if not self:
            return False

        sync_service = self.env['tn.sync.service']
        grouped = {}
        for rec in self:
            if not rec.tn_product_id:
                raise UserError(_('La publicación no está sincronizada con TiendaNube. Exporte primero la publicación.'))
            key = (rec.tn_config_id.id or 0, rec.tn_product_id)
            grouped.setdefault(key, self.env['tn.publication'])
            grouped[key] |= rec

        for (config_id, tn_product_id), pubs in grouped.items():
            siblings = self.search([('tn_product_id', '=', tn_product_id)])
            siblings.write({'published': True})

            tn_config = pubs[0].tn_config_id if pubs and pubs[0].tn_config_id else None
            try:
                sync_service._set_product_published(tn_product_id, True, tn_config=tn_config)
            except Exception as e:
                _logger.exception("Error al activar publicación en TiendaNube: %s", e)
                raise UserError(_('Error al activar en TiendaNube: %s') % str(e))

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Activada'),
                'message': _('La publicación se ha activado en TiendaNube'),
                'type': 'success',
                'sticky': False,
            }
        }

    def _refresh_published_from_tn(self):
        """
        Actualiza el campo 'published' en Odoo consultando el estado actual en TiendaNube.
        Actualiza esta publicación y todas las del mismo tn_product_id (mismo producto en TN).
        Así, si se pausa/activa desde TN, en Odoo se muestra el botón correcto (Pausar/Activar).
        """
        if not self.tn_product_id:
            return
        config = self.tn_config_id or self.env['tn.config'].get_config()
        if not config:
            return
        sync_service = self.env['tn.sync.service']
        published = sync_service.get_product_published(self.tn_product_id, tn_config=config)
        if published is None:
            return
        siblings = self.search([('tn_product_id', '=', self.tn_product_id)])
        if siblings and siblings[0].published != published:
            siblings.write({'published': published})
            _logger.info("✅ Estado 'published' actualizado desde TN: tn_product_id=%s, published=%s", self.tn_product_id, published)

    def action_refresh_published_from_tn(self):
        """
        Acción para actualizar el estado publicado desde TiendaNube.
        Útil cuando se pausa o activa el producto desde TN y se quiere que en Odoo aparezca el botón correcto.
        """
        seen_products = set()
        for rec in self:
            if not rec.tn_product_id or rec.tn_product_id in seen_products:
                continue
            seen_products.add(rec.tn_product_id)
            rec._refresh_published_from_tn()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Estado actualizado'),
                'message': _('Se ha actualizado el estado publicado desde TiendaNube.'),
                'type': 'success',
                'sticky': False,
            }
        }

    def _sync_stock_and_price_from_odoo(self):
        """Sincroniza stock y precio de las variantes desde el producto relacionado de Odoo.

        - Stock: se hereda del almacén configurado en tn.config.
                 Si no hay almacén configurado, usa qty_available total.
        - Precio: se toma desde la lista de precios configurada en tn.config;
                  si no está configurada o falla, se usa list_price de la variante de Odoo.
        """
        self.ensure_one()
        if not self.odoo_product_id:
            _logger.warning("⚠️ No hay producto de Odoo relacionado para sincronizar stock")
            return

        # Obtener configuración
        config = self.tn_config_id if self.tn_config_id else self.env['tn.config'].get_config()
        
        # Obtener la ubicación de stock del almacén configurado
        stock_location = None
        if config and config.warehouse_id:
            # Usar la ubicación de stock del almacén (lot_stock_id)
            stock_location = config.warehouse_id.lot_stock_id
            _logger.info("🏭 Usando almacén configurado: %s (ID: %s)", config.warehouse_id.name, config.warehouse_id.id)
            _logger.info("📍 Ubicación de stock del almacén: %s (ID: %s)", stock_location.name if stock_location else 'N/A', stock_location.id if stock_location else 'N/A')
        elif config and config.stock_location_id:
            # Fallback: usar stock_location_id si warehouse_id no está configurado
            stock_location = config.stock_location_id
            _logger.info("📍 Usando ubicación de stock configurada directamente: %s (ID: %s)", stock_location.name, stock_location.id)
        
        pricelist = config.pricelist_id if config else None

        _logger.info("=" * 80)
        _logger.info("🔄 INICIO: Sincronización de stock desde Odoo")
        _logger.info("📦 Publicación: %s", self.title)
        _logger.info("📦 Producto Odoo: %s (ID: %s)", self.odoo_product_id.name, self.odoo_product_id.id)
        _logger.info("📍 Ubicación de stock final: %s (ID: %s)", 
                    stock_location.name if stock_location else 'Ninguno (usará stock total)', 
                    stock_location.id if stock_location else 'N/A')

        for tn_variant in self.variant_ids:
            if not tn_variant.odoo_variant_id:
                _logger.info("⏭️  Saltando variante '%s' (sin producto Odoo relacionado)", tn_variant.name or 'sin nombre')
                continue

            odoo_var = tn_variant.odoo_variant_id
            stock_anterior = tn_variant.stock or 0.0
            
            _logger.info("-" * 80)
            _logger.info("📦 Procesando variante: %s", tn_variant.name or 'sin nombre')
            _logger.info("   - Variante TN ID: %s", tn_variant.tn_variant_id or 'N/A')
            _logger.info("   - Variante Odoo: %s (ID: %s)", odoo_var.name, odoo_var.id)
            _logger.info("   - Stock ANTERIOR en publicación: %s", stock_anterior)

            # Stock desde el almacén configurado
            try:
                stock_value = 0.0
                # Obtener tipo de stock desde la configuración
                stock_type = config.stock_type if config else 'available'
                use_expected = (stock_type == 'expected')
                
                if stock_location:
                    # Leer stock desde el almacén específico usando stock.quant
                    # Usar child_of para incluir ubicaciones hijas del almacén
                    StockQuant = self.env['stock.quant']
                    quants = StockQuant.search([
                        ('product_id', '=', odoo_var.id),
                        ('location_id', 'child_of', stock_location.id),
                    ])
                    
                    _logger.info("   🔍 Buscando stock en almacén '%s' (ID: %s) y ubicaciones hijas", 
                               stock_location.name, stock_location.id)
                    _logger.info("   📊 Quants encontrados: %d", len(quants))
                    _logger.info("   📊 Tipo de stock: %s", 'Esperado' if use_expected else 'Disponible')
                    
                    for quant in quants:
                        _logger.info("      - Quant: ubicación=%s (ID: %s), cantidad=%s", 
                                   quant.location_id.name, quant.location_id.id, quant.quantity)
                    
                    if use_expected:
                        # Stock esperado: usar virtual_available que considera entradas esperadas y reservas
                        stock_value = float(odoo_var.with_context(location=stock_location.id).virtual_available or 0.0)
                        _logger.info("   ✅ Stock ESPERADO desde almacén '%s': %s (virtual_available)", stock_location.name, stock_value)
                    else:
                        # Stock disponible: usar cantidad física
                        stock_value = sum(quants.mapped('quantity'))
                        _logger.info("   ✅ Stock DISPONIBLE desde almacén '%s': %s", stock_location.name, stock_value)
                else:
                    # Si no hay almacén configurado, usar stock total
                    if use_expected:
                        stock_value = float(odoo_var.virtual_available or 0.0)
                        _logger.info("   📊 Stock ESPERADO total (virtual_available) para variante %s: %s", 
                                    odoo_var.name, stock_value)
                    else:
                        stock_value = float(odoo_var.qty_available or 0.0)
                        _logger.info("   📊 Stock DISPONIBLE total (qty_available) para variante %s: %s", 
                                    odoo_var.name, stock_value)
                
                # Convertir a entero para TiendaNube (no acepta decimales en stock)
                stock_value_int = int(stock_value)
                _logger.info("   🔄 Conversión: %s (float) -> %s (int)", stock_value, stock_value_int)
                
                # Actualizar el stock en la variante de la publicación usando write para asegurar que se guarde
                # IMPORTANTE: Esto actualiza con el valor ABSOLUTO del stock actual en Odoo, no suma ni resta
                _logger.info("   💾 Guardando stock en variante de publicación...")
                _logger.info("      - Valor a guardar: %s (int) - VALOR ABSOLUTO desde Odoo", stock_value_int)
                tn_variant.write({'stock': float(stock_value_int)})
                
                # Invalidar el cache para leer el valor actualizado
                tn_variant.invalidate_recordset(['stock'])
                
                # Leer el valor después de guardar
                stock_despues = tn_variant.stock or 0.0
                _logger.info("   ✅ Stock DESPUÉS en publicación: %s", stock_despues)
                _logger.info("   📈 Cambio: %s -> %s (diferencia: %s)", 
                           stock_anterior, stock_despues, stock_despues - stock_anterior)
                
            except Exception as e:
                # Si por alguna razón falla, no romper la exportación
                _logger.exception("⚠️ Error leyendo stock para variante Odoo ID %s (almacén: %s): %s", 
                              odoo_var.id, stock_location.name if stock_location else 'N/A', str(e))
                # No establecer a 0 si ya tiene un valor, solo loguear el error
                if tn_variant.stock is None or tn_variant.stock == 0:
                    tn_variant.write({'stock': 0.0})
                    _logger.warning("   ⚠️ Stock establecido a 0 debido al error")
                else:
                    _logger.warning("   ⚠️ Error al actualizar stock, manteniendo valor anterior: %s", tn_variant.stock)

            # Precio desde la lista de precios configurada (si existe)
            price = odoo_var.list_price
            if pricelist:
                try:
                    # Usar _get_product_price que es el método correcto en Odoo
                    price = pricelist._get_product_price(odoo_var.product_tmpl_id, 1.0)
                    if not price:
                        price = odoo_var.list_price
                    _logger.debug("💰 Precio desde lista '%s' para variante %s: %s", 
                                pricelist.name, odoo_var.id, price)
                except Exception as e:
                    _logger.warning("⚠️ Error obteniendo precio desde lista '%s' para variante Odoo ID %s: %s",
                                  pricelist.name, odoo_var.id, str(e))
                    # Usar precio de lista como fallback
                    price = odoo_var.list_price
            else:
                _logger.debug("💰 No hay lista de precios configurada, usando list_price: %s", price)

            tn_variant.price = price
    
    def _get_stock_total_for_pause_check(self):
        """
        Calcula el stock total (suma de variantes) usando el tipo de stock de la configuración:
        - Stock Disponible (available): cantidad física en ubicación/almacén.
        - Stock Esperado (expected): virtual_available (considera entradas esperadas y reservas).
        """
        self.ensure_one()
        config = self.tn_config_id if self.tn_config_id else self.env['tn.config'].get_config()
        if not config:
            return 0.0
        stock_location = None
        if config.warehouse_id and config.warehouse_id.lot_stock_id:
            stock_location = config.warehouse_id.lot_stock_id
        elif config.stock_location_id:
            stock_location = config.stock_location_id
        stock_type = config.stock_type or 'available'
        use_expected = (stock_type == 'expected')
        total = 0.0
        for variant in self.variant_ids:
            if not variant.odoo_variant_id:
                continue
            odoo_var = variant.odoo_variant_id
            if use_expected:
                if stock_location:
                    total += float(odoo_var.with_context(location=stock_location.id).virtual_available or 0.0)
                else:
                    total += float(odoo_var.virtual_available or 0.0)
            else:
                if stock_location:
                    quants = self.env['stock.quant'].search([
                        ('product_id', '=', odoo_var.id),
                        ('location_id', 'child_of', stock_location.id),
                    ])
                    total += sum(quants.mapped('quantity'))
                else:
                    total += float(odoo_var.qty_available or 0.0)
        return total

    def _check_and_update_published_status(self):
        """
        Verifica el stock total y pausa/despausa según configuración.
        El stock usado es el de la configuración: Disponible (físico) o Esperado (virtual_available).
        - Si enable_min_stock_pause y stock < umbral_pausa -> pausar (published=False).
        - Si enable_min_stock_unpause y stock >= umbral_despausa -> despausar (published=True).
        """
        self.ensure_one()
        
        _logger.info("=" * 80)
        _logger.info("🔍 INICIO: Verificación de stock mínimo para pausar/despausar")
        _logger.info("📦 Publicación: %s (ID: %s, TN Product ID: %s)", self.title, self.id, self.tn_product_id)
        
        # Obtener configuración
        config = self.tn_config_id if self.tn_config_id else self.env['tn.config'].get_config()
        if not config:
            _logger.warning("⚠️ No hay configuración de TiendaNube, omitiendo verificación de stock mínimo")
            _logger.info("=" * 80)
            return
        
        _logger.info("⚙️ Configuración: %s (ID: %s)", config.name, config.id)
        _logger.info("   - enable_min_stock_pause (global): %s", config.enable_min_stock_pause)
        _logger.info("   - enable_min_stock_unpause (global): %s", config.enable_min_stock_unpause)
        _logger.info("   - global_min_stock_pause: %s", config.global_min_stock_pause)
        _logger.info("   - global_min_stock_unpause: %s", config.global_min_stock_unpause)
        
        # Determinar si la pausa y despausa automática están activadas
        # Las reglas específicas de la publicación PISAN (sobrescriben) las globales.
        # Si hay configuración específica (checkbox marcado o umbral > 0), usar esa.
        # Si no hay configuración específica, usar la global.
        
        _logger.info("📋 Valores específicos de la publicación:")
        _logger.info("   - enable_min_stock_pause (específico): %s", self.enable_min_stock_pause)
        _logger.info("   - enable_min_stock_unpause (específico): %s", self.enable_min_stock_unpause)
        _logger.info("   - min_stock_pause (específico): %s", self.min_stock_pause)
        _logger.info("   - min_stock_unpause (específico): %s", self.min_stock_unpause)
        
        # Determinar si hay configuración específica para pausa:
        # - Si enable_min_stock_pause está True → hay configuración específica
        # - Si min_stock_pause > 0 → hay configuración específica (umbral configurado)
        # - Si ambos son False/0 → usar configuración global
        has_specific_pause_config = self.enable_min_stock_pause or (self.min_stock_pause and self.min_stock_pause > 0)
        has_specific_unpause_config = self.enable_min_stock_unpause or (self.min_stock_unpause and self.min_stock_unpause > 0)
        
        _logger.info("🔍 ¿Hay configuración específica?")
        _logger.info("   - Pausa específica configurada: %s", has_specific_pause_config)
        _logger.info("   - Despausa específica configurada: %s", has_specific_unpause_config)
        
        # Si hay configuración específica, usar el checkbox específico (True o False)
        # Si NO hay configuración específica, usar el checkbox global
        if has_specific_pause_config:
            pause_enabled = self.enable_min_stock_pause
            _logger.info("   ✅ Usando configuración ESPECÍFICA para pausa: %s", pause_enabled)
        else:
            pause_enabled = config.enable_min_stock_pause
            _logger.info("   ✅ Usando configuración GLOBAL para pausa: %s", pause_enabled)
        
        if has_specific_unpause_config:
            unpause_enabled = self.enable_min_stock_unpause
            _logger.info("   ✅ Usando configuración ESPECÍFICA para despausa: %s", unpause_enabled)
        else:
            unpause_enabled = config.enable_min_stock_unpause
            _logger.info("   ✅ Usando configuración GLOBAL para despausa: %s", unpause_enabled)
        
        _logger.info("✅ Funcionalidades activadas (después de aplicar específico/global):")
        _logger.info("   - pause_enabled: %s", pause_enabled)
        _logger.info("   - unpause_enabled: %s", unpause_enabled)
        
        # Si ninguna funcionalidad está activada, no hacer nada
        if not pause_enabled and not unpause_enabled:
            _logger.warning("⚠️ Control de stock mínimo desactivado (pausa y despausa desactivadas), omitiendo verificación")
            _logger.info("=" * 80)
            return
        
        # Obtener umbrales: usar específicos de la publicación si están configurados (> 0), sino usar globales
        # Si hay configuración específica (has_specific_*_config), usar el umbral específico si está configurado (> 0)
        # Si el umbral específico no está configurado (0 o None) pero hay config específica, usar el global como fallback
        # Si NO hay configuración específica, usar siempre el global
        if has_specific_pause_config:
            # Hay config específica: usar umbral específico si está configurado, sino usar global como fallback
            pause_threshold = self.min_stock_pause if (self.min_stock_pause and self.min_stock_pause > 0) else config.global_min_stock_pause
            _logger.info("   📊 Umbral pausa: usando ESPECÍFICO (%s) o GLOBAL como fallback (%s)", self.min_stock_pause, config.global_min_stock_pause)
        else:
            # No hay config específica: usar siempre el global
            pause_threshold = config.global_min_stock_pause
            _logger.info("   📊 Umbral pausa: usando GLOBAL (%s)", pause_threshold)
        
        if has_specific_unpause_config:
            # Hay config específica: usar umbral específico si está configurado, sino usar global como fallback
            unpause_threshold = self.min_stock_unpause if (self.min_stock_unpause and self.min_stock_unpause > 0) else config.global_min_stock_unpause
            _logger.info("   📊 Umbral despausa: usando ESPECÍFICO (%s) o GLOBAL como fallback (%s)", self.min_stock_unpause, config.global_min_stock_unpause)
        else:
            # No hay config específica: usar siempre el global
            unpause_threshold = config.global_min_stock_unpause
            _logger.info("   📊 Umbral despausa: usando GLOBAL (%s)", unpause_threshold)
        
        _logger.info("📊 Umbrales finales (después de aplicar específico/global):")
        _logger.info("   - pause_threshold: %s", pause_threshold)
        _logger.info("   - unpause_threshold: %s", unpause_threshold)
        
        # Verificar si los umbrales necesarios están configurados según qué funcionalidad está activada
        if pause_enabled and pause_threshold <= 0:
            _logger.warning("⚠️ Pausa automática activada pero umbral de pausa no configurado (<= 0), omitiendo pausa")
            pause_enabled = False
        
        if unpause_enabled and unpause_threshold <= 0:
            _logger.warning("⚠️ Despausa automática activada pero umbral de despausa no configurado (<= 0), omitiendo despausa")
            unpause_enabled = False
        
        # Si ninguna funcionalidad está realmente activada (por falta de umbrales), no hacer nada
        if not pause_enabled and not unpause_enabled:
            _logger.warning("⚠️ Ninguna funcionalidad de control de stock mínimo está activa, omitiendo verificación")
            _logger.info("=" * 80)
            return
        
        # Calcular stock total según config: Disponible o Esperado
        total_stock = self._get_stock_total_for_pause_check()
        stock_type = (config.stock_type or 'available')
        _logger.info("📦 Stock total para regla de pausa (%s): %s", 'Esperado' if stock_type == 'expected' else 'Disponible', total_stock)
        
        _logger.info("📊 RESUMEN DE VERIFICACIÓN:")
        _logger.info("   - Stock total final: %s", total_stock)
        _logger.info("   - Pausa automática: %s (%s)", 'ACTIVADA' if pause_enabled else 'DESACTIVADA', 'específica' if self.enable_min_stock_pause else 'global')
        _logger.info("   - Despausa automática: %s (%s)", 'ACTIVADA' if unpause_enabled else 'DESACTIVADA', 'específica' if self.enable_min_stock_unpause else 'global')
        _logger.info("   - Umbral pausa: %s (%s)", pause_threshold, 'específico' if (self.min_stock_pause and self.min_stock_pause > 0) else 'global')
        _logger.info("   - Umbral despausa: %s (%s)", unpause_threshold, 'específico' if (self.min_stock_unpause and self.min_stock_unpause > 0) else 'global')
        _logger.info("   - Estado actual (published): %s", self.published)
        
        # Determinar nuevo estado: pausa automática (si enable_min_stock_pause) y despausa automática (si enable_min_stock_unpause)
        new_published_status = self.published
        action_taken = None
        
        # Verificar pausa solo si está activada (enable_min_stock_pause)
        if pause_enabled:
            _logger.info("🔍 Verificando condición de PAUSA: total_stock (%s) < pause_threshold (%s) = %s", 
                       total_stock, pause_threshold, total_stock < pause_threshold)
            if total_stock < pause_threshold:
                _logger.info("   ✅ Condición de pausa CUMPLIDA: %s < %s", total_stock, pause_threshold)
                if self.published:
                    new_published_status = False
                    action_taken = "pausar"
                    _logger.info("   ⏸️  Stock por debajo del umbral de pausa (%s < %s) -> PAUSAR", total_stock, pause_threshold)
                else:
                    _logger.info("   ℹ️  Ya está pausada, no se requiere acción")
            else:
                _logger.info("   ❌ Condición de pausa NO cumplida: %s >= %s", total_stock, pause_threshold)
        else:
            _logger.info("   ⏭️  Pausa automática DESACTIVADA (enable_min_stock_pause), omitiendo verificación de pausa")
        
        # Verificar despausa solo si está activada (activar cuando stock >= umbral)
        if unpause_enabled:
            _logger.info("🔍 Verificando condición de DESPAUSA: total_stock (%s) >= unpause_threshold (%s) = %s", 
                       total_stock, unpause_threshold, total_stock >= unpause_threshold)
            if total_stock >= unpause_threshold:
                _logger.info("   ✅ Condición de despausa CUMPLIDA: %s >= %s", total_stock, unpause_threshold)
                if not self.published:
                    new_published_status = True
                    action_taken = "despausar"
                    _logger.info("   ▶️  Stock por encima del umbral de despausa (%s >= %s) -> DESPAUSAR", total_stock, unpause_threshold)
                else:
                    _logger.info("   ℹ️  Ya está publicada, no se requiere acción")
            else:
                _logger.info("   ❌ Condición de despausa NO cumplida: %s < %s", total_stock, unpause_threshold)
        else:
            _logger.info("   ⏭️  Despausa automática DESACTIVADA, omitiendo verificación de despausa")
        
        # Actualizar estado si cambió
        _logger.info("💾 DECISIÓN FINAL:")
        _logger.info("   - Estado anterior: %s", self.published)
        _logger.info("   - Estado nuevo: %s", new_published_status)
        _logger.info("   - Acción: %s", action_taken or 'NINGUNA')
        
        if new_published_status != self.published:
            _logger.info("   ✅ Cambio de estado requerido: %s -> %s", self.published, new_published_status)
            try:
                self.write({'published': new_published_status})
                _logger.info("   ✅ Estado actualizado en Odoo")
                
                # Sincronizar el cambio a TiendaNube si la publicación ya está sincronizada
                if self.tn_product_id:
                    try:
                        sync_service = self.env['tn.sync.service']
                        _logger.info("   📤 Sincronizando cambio de estado a TiendaNube (product_id: %s)...", self.tn_product_id)
                        # Actualizar solo el campo published en TiendaNube
                        sync_service.export_publication(self)
                        _logger.info("   ✅ Estado sincronizado a TiendaNube: published=%s", new_published_status)
                    except Exception as e:
                        _logger.exception("   ❌ Error sincronizando estado a TiendaNube: %s", e)
                        # No fallar si falla la sincronización, solo loguear
                else:
                    _logger.warning("   ⚠️  Publicación aún no sincronizada con TiendaNube (sin tn_product_id), estado guardado solo localmente")
            except Exception as e:
                _logger.exception("   ❌ Error actualizando estado en Odoo: %s", e)
        else:
            _logger.info("   ℹ️  No se requiere cambio de estado (ya está en el estado correcto)")
        
        _logger.info("=" * 80)
    
    def action_sync_from_odoo_product(self):
        """Sincroniza desde un producto de Odoo"""
        self.ensure_one()
        if not self.odoo_product_id:
            raise UserError(_('Debe seleccionar un producto de Odoo primero'))
        
        product = self.odoo_product_id
        
        # Obtener dimensiones desde la primera variante usando getattr para evitar errores si los campos no existen
        # En algunas versiones de Odoo estos campos pueden no estar disponibles
        first_variant = product.product_variant_ids[0] if product.product_variant_ids else None
        if first_variant:
            weight = getattr(first_variant, 'weight', None) or 0.0
            width = getattr(first_variant, 'width', None) or 0.0
            height = getattr(first_variant, 'height', None) or 0.0
            depth = getattr(first_variant, 'length', None) or 0.0
        else:
            # Si no hay variantes, usar valores por defecto
            weight = width = height = depth = 0.0
        
        # Actualizar campos básicos
        # IMPORTANTE: Solo actualizar title si está vacío, para no sobrescribir el título personalizado
        vals = {
            'title': self.title or product.name,  # Mantener título personalizado si existe, sino usar nombre del producto
            'description': product.description or '',
            'description_text': product.description_sale or '',
            # Dimensiones físicas desde la primera variante del producto
            'weight': weight,  # peso en kg
            'width': width,     # ancho en cm
            'height': height,   # alto en cm
            'depth': depth,     # profundidad (length en Odoo) en cm
        }
        
        # Sincronizar variantes usando el método reutilizable
        self._sync_variants_from_odoo_product()
        
        # Sincronizar imágenes
        if product.image_1920:
            # Eliminar imágenes existentes
            self.image_ids.unlink()
            
            # Crear imagen principal
            self.env['tn.publication.image'].create({
                'publication_id': self.id,
                'image': product.image_1920,
                'position': 1,
            })
        
        self.write(vals)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Sincronización completada'),
                'message': _('Datos sincronizados desde el producto de Odoo'),
                'type': 'success',
                'sticky': False,
            }
        }
        
        # Sincronizar imágenes
        if product.image_1920:
            # Eliminar imágenes existentes
            self.image_ids.unlink()
            
            # Crear imagen principal
            self.env['tn.publication.image'].create({
                'publication_id': self.id,
                'image': product.image_1920,
                'position': 1,
            })
        
        self.write(vals)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Sincronización completada'),
                'message': _('Datos sincronizados desde el producto de Odoo'),
                'type': 'success',
                'sticky': False,
            }
        }


class TNPublicationImage(models.Model):
    """Imágenes de publicaciones de TiendaNube"""
    _name = 'tn.publication.image'
    _description = 'Imagen de Publicación TiendaNube'
    _order = 'position, id'

    publication_id = fields.Many2one(
        'tn.publication',
        string='Publicación',
        required=True,
        ondelete='cascade'
    )
    
    name = fields.Char(string='Nombre', help='Nombre de la imagen')
    image = fields.Binary(string='Imagen', required=True, help='Imagen del producto')
    image_url = fields.Char(string='URL Externa', help='URL de la imagen en TiendaNube')
    position = fields.Integer(string='Posición', default=1, help='Orden de la imagen')
    tn_image_id = fields.Integer(string='ID Imagen TiendaNube', help='ID de la imagen en TiendaNube')
    
    def unlink(self):
        """Elimina la imagen de TiendaNube cuando se elimina desde Odoo"""
        # Obtener IDs de imágenes y productos antes de eliminar
        images_to_delete = []
        for img in self:
            if img.tn_image_id and img.publication_id and img.publication_id.tn_product_id:
                images_to_delete.append({
                    'image_id': img.tn_image_id,
                    'product_id': img.publication_id.tn_product_id,
                })
        
        # Eliminar desde Odoo primero
        result = super(TNPublicationImage, self).unlink()
        
        # Eliminar de TiendaNube
        if images_to_delete:
            try:
                sync_service = self.env['tn.sync.service']
                for img_data in images_to_delete:
                    try:
                        sync_service.delete_image_from_tn(
                            img_data['product_id'],
                            img_data['image_id']
                        )
                        _logger.info("✅ Imagen eliminada de TiendaNube: product_id=%s, image_id=%s", 
                                   img_data['product_id'], img_data['image_id'])
                    except Exception as e:
                        _logger.warning("⚠️ Error al eliminar imagen de TiendaNube (product_id=%s, image_id=%s): %s", 
                                      img_data['product_id'], img_data['image_id'], str(e))
                        # No fallar la eliminación en Odoo si falla en TiendaNube
            except Exception as e:
                _logger.warning("⚠️ Error general al eliminar imágenes de TiendaNube: %s", str(e))
        
        return result


class TNPublicationVariant(models.Model):
    """Variantes de publicaciones de TiendaNube"""
    _name = 'tn.publication.variant'
    _description = 'Variante de Publicación TiendaNube'
    _order = 'id'

    publication_id = fields.Many2one(
        'tn.publication',
        string='Publicación',
        required=True,
        ondelete='cascade'
    )
    
    odoo_variant_id = fields.Many2one(
        'product.product',
        string='Variante Odoo',
        ondelete='set null',
        domain="[('product_tmpl_id', '=', parent.odoo_product_id)]",
        help='Variante de producto de Odoo asociada'
    )
    
    name = fields.Char(string='Nombre', help='Nombre de la variante/atributo')
    sku = fields.Char(string='SKU', help='Código SKU de la variante')
    price = fields.Float(string='Precio', required=True, help='Precio de la variante')
    stock = fields.Float(string='Stock', default=0.0, help='Cantidad disponible')
    
    # Atributos de variante (nombres de las opciones)
    option1_name = fields.Char(string='Opción 1', help='Nombre de la primera opción de variante (ej: Talla)')
    option1_value = fields.Char(string='Valor Opción 1', help='Valor de la primera opción (ej: M)')
    option2_name = fields.Char(string='Opción 2', help='Nombre de la segunda opción de variante (ej: Color)')
    option2_value = fields.Char(string='Valor Opción 2', help='Valor de la segunda opción (ej: Rojo)')
    option3_name = fields.Char(string='Opción 3', help='Nombre de la tercera opción de variante')
    option3_value = fields.Char(string='Valor Opción 3', help='Valor de la tercera opción')
    
    weight = fields.Float(string='Peso (kg)', help='Peso de la variante en kilogramos')
    width = fields.Float(string='Ancho (cm)', help='Ancho de la variante en centímetros')
    height = fields.Float(string='Alto (cm)', help='Alto de la variante en centímetros')
    depth = fields.Float(string='Profundidad (cm)', help='Profundidad de la variante en centímetros')
    barcode = fields.Char(string='Código de Barras', help='Código de barras de la variante (GTIN, EAN, ISBN, etc.)')
    
    tn_variant_id = fields.Integer(string='ID Variante TiendaNube', help='ID de la variante en TiendaNube')
    
    active = fields.Boolean(string='Activo', default=True)
    
    @api.onchange('odoo_variant_id')
    def _onchange_odoo_variant_id(self):
        """Cuando se selecciona una variante de Odoo, matchear automáticamente los atributos"""
        if not self.odoo_variant_id:
            return
        
        variant = self.odoo_variant_id
        
        # Obtener atributos de la variante de Odoo
        attr_values = variant.product_template_attribute_value_ids
        
        # Limpiar campos de opciones
        self.option1_name = ''
        self.option1_value = ''
        self.option2_name = ''
        self.option2_value = ''
        self.option3_name = ''
        self.option3_value = ''
        
        # Llenar hasta 3 atributos
        if attr_values:
            attrs = list(attr_values)[:3]
            if len(attrs) >= 1:
                self.option1_name = attrs[0].attribute_id.name or ''
                self.option1_value = attrs[0].name or ''
            if len(attrs) >= 2:
                self.option2_name = attrs[1].attribute_id.name or ''
                self.option2_value = attrs[1].name or ''
            if len(attrs) >= 3:
                self.option3_name = attrs[2].attribute_id.name or ''
                self.option3_value = attrs[2].name or ''
        
        # Construir nombre legible de la variante
        name_parts = []
        if self.option1_name and self.option1_value:
            name_parts.append(f"{self.option1_name}: {self.option1_value}")
        if self.option2_name and self.option2_value:
            name_parts.append(f"{self.option2_name}: {self.option2_value}")
        if self.option3_name and self.option3_value:
            name_parts.append(f"{self.option3_name}: {self.option3_value}")
        
        if name_parts:
            self.name = ' / '.join(name_parts)
        else:
            self.name = variant.display_name or variant.name or 'Variante'
        
        # Sincronizar otros campos desde la variante de Odoo
        if not self.sku:
            self.sku = variant.default_code or ''
        if not self.price:
            self.price = variant.list_price or 0.0
        
        # Sincronizar dimensiones si están disponibles
        if hasattr(variant, 'weight') and variant.weight:
            self.weight = variant.weight
        if hasattr(variant, 'width') and variant.width:
            self.width = variant.width
        if hasattr(variant, 'height') and variant.height:
            self.height = variant.height
        if hasattr(variant, 'length') and variant.length:
            self.depth = variant.length
        if hasattr(variant, 'barcode') and variant.barcode:
            self.barcode = variant.barcode
        
        # Sincronizar stock desde Odoo según el tipo configurado
        config = self.publication_id.tn_config_id if self.publication_id and self.publication_id.tn_config_id else self.env['tn.config'].get_config()
        stock_type = config.stock_type if config else 'available'
        use_expected = (stock_type == 'expected')
        
        if hasattr(variant, 'qty_available'):
            if use_expected:
                self.stock = variant.virtual_available or 0.0
            else:
                self.stock = variant.qty_available or 0.0


class TNPublicationAttribute(models.Model):
    """Campos personalizados de productos TiendaNube (Product Custom Fields).

    Basado en la documentación oficial de TiendaNube:
    https://tiendanube.github.io/api-documentation/resources/products/custom-fields
    """
    _name = 'tn.publication.attribute'
    _description = 'Campo personalizado de producto TiendaNube'
    _order = 'sequence, id'

    publication_id = fields.Many2one(
        'tn.publication',
        string='Publicación',
        required=True,
        ondelete='cascade'
    )
    
    name = fields.Char(
        string='Nombre',
        required=True,
        help='Nombre del campo personalizado (coincide con el name del custom field en TiendaNube)'
    )
    value = fields.Char(
        string='Valor',
        required=True,
        help='Valor asociado al campo personalizado para este producto'
    )
    sequence = fields.Integer(string='Secuencia', default=10, help='Orden de visualización')
    
    # UUID del custom field en TiendaNube (products/custom-fields)
    tn_custom_field_id = fields.Char(
        string='ID Campo Personalizado TiendaNube',
        help='UUID del custom field en TiendaNube (products/custom-fields)'
    )

    # Campo legacy (no se usa más, se mantiene por compatibilidad)
    tn_attribute_id = fields.Integer(
        string='ID Atributo TiendaNube (legacy)',
        help='Campo legacy, no se utiliza para campos personalizados actuales'
    )


class TNPublicationTag(models.Model):
    """Tags de publicaciones de TiendaNube"""
    _name = 'tn.publication.tag'
    _description = 'Tag de Publicación TiendaNube'
    _rec_name = 'name'

    name = fields.Char(string='Tag', required=True, help='Nombre del tag')
    tn_tag_id = fields.Char(string='ID Tag TiendaNube', help='ID del tag en TiendaNube')
    
    publication_ids = fields.Many2many(
        'tn.publication',
        'tn_publication_tag_rel',
        'tag_id',
        'publication_id',
        string='Publicaciones',
        help='Publicaciones con este tag'
    )


class TNPublicationCategory(models.Model):
    """Categorías de publicaciones de TiendaNube"""
    _name = 'tn.publication.category'
    _description = 'Categoría de Publicación TiendaNube'
    _rec_name = 'name'

    name = fields.Char(string='Categoría', required=True, help='Nombre de la categoría')
    tn_category_id = fields.Integer(string='ID Categoría TiendaNube', help='ID de la categoría en TiendaNube')
    handle = fields.Char(string='Handle', help='Handle de la categoría en TiendaNube')
    
    publication_ids = fields.Many2many(
        'tn.publication',
        'tn_publication_category_rel',
        'category_id',
        'publication_id',
        string='Publicaciones',
        help='Publicaciones en esta categoría'
    )


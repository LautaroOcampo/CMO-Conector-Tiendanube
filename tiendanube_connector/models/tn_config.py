# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError
import logging

_logger = logging.getLogger(__name__)


class TNConfig(models.Model):
    """Configuración de TiendaNube como modelo normal (no settings)"""
    _name = 'tn.config'
    _description = 'Configuración TiendaNube'
    _rec_name = 'name'

    name = fields.Char(
        string='Nombre de la Cuenta',
        required=True,
        default='Cuenta TiendaNube',
        help='Nombre para identificar esta cuenta de TiendaNube'
    )

    # Credenciales TiendaNube
    client_id = fields.Char(
        string='Client ID',
        help='Client ID de la aplicación TiendaNube'
    )
    
    client_secret = fields.Char(
        string='Client Secret',
        help='Client Secret de la aplicación TiendaNube'
    )
    
    access_token = fields.Char(
        string='Access Token',
        help='Access Token de TiendaNube'
    )
    
    refresh_token = fields.Char(
        string='Refresh Token',
        help='Refresh Token de TiendaNube'
    )
    
    store_id = fields.Char(
        string='Store ID',
        help='ID de la tienda en TiendaNube'
    )
    
    api_base_url = fields.Char(
        string='API Base URL',
        default='https://api.tiendanube.com/v1',
        help='URL base de la API de TiendaNube'
    )
    
    company_id = fields.Many2one(
        'res.company',
        string='Compañía',
        default=lambda self: self.env.company,
        required=True
    )
    
    # Configuración de sincronización
    stock_location_id = fields.Many2one(
        'stock.location',
        string='Almacén de Stock',
        domain=[('usage', '=', 'internal')],
        help='Almacén de Odoo desde el cual se leerá el stock para sincronizar con TiendaNube. Solo se muestran ubicaciones de tipo "interno".'
    )
    
    stock_type = fields.Selection(
        [
            ('available', 'Stock Disponible'),
            ('expected', 'Stock Esperado'),
        ],
        string="Tipo de Stock a Sincronizar",
        default='available',
        required=True,
        help="Stock Disponible: Stock físico disponible. Stock Esperado: Stock disponible menos reservado más entradas esperadas."
    )
    
    pricelist_id = fields.Many2one(
        'product.pricelist',
        string='Lista de Precios',
        help='Lista de precios de Odoo que se usará para sincronizar precios con TiendaNube. Si no se selecciona, se usará el precio de lista (list_price).'
    )
    
    sale_pricelist_id = fields.Many2one(
        'product.pricelist',
        string='Lista de Precios para Ventas',
        help='Lista de precios que se usará al crear órdenes de venta en Odoo desde TiendaNube.'
    )

    fallback_product_id = fields.Many2one(
        'product.product',
        string='Producto por Defecto (Errores SKU)',
        domain=[('active', '=', True)],
        help='Producto a usar cuando no se pueda mapear una línea de TiendaNube por publicación/SKU.'
    )
    
    invoice_journal_id = fields.Many2one(
        'account.journal',
        string='Diario Predeterminado para Facturas',
        domain=[('type', '=', 'sale')],
        help='Diario que se usará al crear facturas automáticamente desde órdenes de TiendaNube.'
    )

    default_income_account_id = fields.Many2one(
        'account.account',
        string='Cuenta de Ingresos Predeterminada',
        domain=[('deprecated', '=', False)],
        help='Cuenta contable de ingresos a usar en líneas de factura de ventas TiendaNube (reemplaza la cuenta por defecto del producto/categoría).'
    )
    
    default_tax_id = fields.Many2one(
        'account.tax',
        string='Impuesto por Defecto',
        domain=[('type_tax_use', '=', 'sale')],
        help='Impuesto que se aplicará por defecto a las líneas de las órdenes de venta creadas desde TiendaNube.'
    )
    
    auto_invoice = fields.Boolean(
        string='Facturar Automáticamente',
        default=False,
        help='Si está activado, se creará y publicará automáticamente la factura al crear la orden de venta desde TiendaNube.'
    )

    consumidor_final_partner_id = fields.Many2one(
        'res.partner',
        string='Contacto Consumidor Final (pedidos sin provincia)',
        domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]",
        help='Si el pedido de TiendaNube no trae provincia de entrega, no se crea contacto nuevo: '
             'la orden de venta y la factura usarán este contacto (p. ej. Consumidor Final). No se modifica el contacto al asignarlo.'
    )
    
    warehouse_id = fields.Many2one(
        'stock.warehouse',
        string='Almacén Predeterminado para Envíos',
        help='Almacén de Odoo que se usará para todos los envíos creados desde TiendaNube.'
    )
    
    webhook_id = fields.Integer(
        string='ID Webhook TiendaNube',
        help='ID del webhook creado en TiendaNube para recibir notificaciones de ventas'
    )
    
    webhook_url = fields.Char(
        string='URL del Webhook',
        readonly=True,
        help='URL del webhook configurado en TiendaNube'
    )
    
    webhook_enabled = fields.Boolean(
        string='Procesar Webhooks de Ventas',
        default=True,
        help='Si está desactivado, los webhooks de ventas seguirán llegando pero no se procesarán. El webhook permanece activo en TiendaNube.'
    )
    
    last_sync_date = fields.Datetime(
        string='Última Sincronización de Órdenes',
        help='Fecha y hora de la última sincronización de órdenes mediante polling. Se actualiza automáticamente cuando el cron ejecuta la sincronización.'
    )
    
    # Configuración de polling de órdenes: solo horas hacia atrás
    order_polling_hours_back = fields.Integer(
        string='Horas hacia atrás (polling de ventas)',
        default=24,
        help='En cada ejecución del cron, se buscan ventas creadas en TiendaNube en las últimas N horas. Se usa el mayor entre esta ventana y la última sincronización. Límite: 1 a 168 horas (7 días).'
    )
    
    # Campos internos (sin UI; se usan valores por defecto)
    polling_orders_per_page = fields.Integer(
        string='Órdenes por Página',
        default=200,
        help='Cantidad de órdenes por página en la API. Uso interno.'
    )
    polling_max_pages = fields.Integer(
        string='Máximo de Páginas por Ejecución',
        default=10,
        help='Máximo de páginas por ejecución. Uso interno.'
    )
    
    polling_enabled = fields.Boolean(
        string='Activar Sincronización Automática por Polling',
        default=True,
        help='Si está activado, el cron ejecutará automáticamente la sincronización de órdenes cada 10 minutos. Si está desactivado, solo se sincronizarán órdenes mediante webhooks.'
    )
    
    def _get_order_polling_hours_back(self):
        """Horas hacia atrás para el polling, respetando límites (1 a 168 = 7 días)."""
        self.ensure_one()
        val = self.order_polling_hours_back or 24
        return max(1, min(168, val))
    
    @api.constrains('order_polling_hours_back')
    def _check_order_polling_hours_back(self):
        for r in self:
            if r.order_polling_hours_back is not False and (r.order_polling_hours_back < 1 or r.order_polling_hours_back > 168):
                raise ValidationError(_('Horas hacia atrás debe estar entre 1 y 168 (7 días).'))
    
    # Timeout y reintentos para llamadas a la API
    api_timeout_seconds = fields.Integer(
        string='Timeout API (segundos)',
        default=30,
        help='Tiempo máximo de espera por petición a la API de TiendaNube. Si no se define, se usa 30 segundos. Reintentos aplican con backoff exponencial (máx. 2 minutos total).'
    )
    api_max_retries = fields.Integer(
        string='Reintentos máximos API',
        default=3,
        help='Número máximo de reintentos ante timeout o error de conexión. Se respeta siempre el rate limit (429).'
    )
    
    # Estado del cron (solo informativo; el cron es global por base de datos)
    cron_orders_next_run = fields.Datetime(
        string='Próxima Ejecución del Cron',
        compute='_compute_cron_orders_status',
        help='Fecha y hora en que el cron de sincronización de órdenes se ejecutará nuevamente.'
    )
    cron_orders_last_run = fields.Datetime(
        string='Última Ejecución del Cron',
        compute='_compute_cron_orders_status',
        help='Fecha y hora de la última ejecución del cron de sincronización de órdenes.'
    )
    cron_orders_active = fields.Boolean(
        string='Cron de Órdenes Activo',
        compute='_compute_cron_orders_status',
        help='Si el cron está activo en Odoo (Configuración > Técnico > Acciones planificadas).'
    )
    
    auto_sync_stock_on_odoo_change = fields.Boolean(
        string='Sincronizar Stock Automáticamente desde Odoo',
        default=False,
        help='Si está activado, cuando se modifique el stock en Odoo (por ventas, compras, ajustes manuales, etc.), se sincronizará automáticamente el stock a TiendaNube. El stock se copia directamente (valor absoluto), no se suma ni resta.'
    )
    
    auto_sync_price_on_odoo_change = fields.Boolean(
        string='Sincronizar Precio Automáticamente desde Odoo',
        default=False,
        help='Si está activado, cuando se modifique el precio del producto en Odoo (list_price o en la lista de precios configurada), se sincronizará automáticamente el precio a TiendaNube.'
    )
    
    # Control de stock mínimo para pausar/despausar publicaciones
    enable_min_stock_pause = fields.Boolean(
        string='Activar Pausa Automática por Stock',
        default=False,
        help='Si está activado, las publicaciones se pausarán automáticamente cuando el stock caiga por debajo del umbral de pausa configurado.'
    )
    
    enable_min_stock_unpause = fields.Boolean(
        string='Activar Despausa Automática por Stock',
        default=False,
        help='Si está activado, las publicaciones se despausarán automáticamente cuando el stock supere el umbral de despausa configurado.'
    )
    
    global_min_stock_pause = fields.Float(
        string='Stock Mínimo para Pausar (Global)',
        default=0.0,
        help='Stock mínimo global. Cuando el stock total de una publicación caiga por debajo de este valor, se pausará automáticamente (si la pausa automática está activada). Las publicaciones con valores específicos usarán sus propios umbrales.'
    )
    
    global_min_stock_unpause = fields.Float(
        string='Stock Mínimo para Despausar (Global)',
        default=1.0,
        help='Stock mínimo global. Cuando el stock total de una publicación supere este valor, se despausará automáticamente (si la despausa automática está activada). Las publicaciones con valores específicos usarán sus propios umbrales.'
    )

    @api.model
    def get_config(self):
        """Obtiene el primer registro de configuración disponible para la compañía actual"""
        config = self.search([('company_id', '=', self.env.company.id)], limit=1)
        if not config:
            # Si no hay configuración, buscar cualquier configuración disponible
            config = self.search([], limit=1)
        return config

    @api.model
    def action_open_config(self):
        """Abre la lista de configuraciones de TiendaNube"""
        return {
            'type': 'ir.actions.act_window',
            'name': 'Configuraciones TiendaNube',
            'res_model': 'tn.config',
            'view_mode': 'list,form',
            'target': 'current',
            'domain': [('company_id', '=', self.env.company.id)],
        }

    def write(self, vals):
        """Al guardar, también actualizar ir.config_parameter y crear webhook automáticamente si corresponde"""
        result = super(TNConfig, self).write(vals)
        
        # Sincronizar con ir.config_parameter
        ICP = self.env['ir.config_parameter'].sudo()
        for record in self:
            if 'client_id' in vals:
                ICP.set_param('tiendanube_connector.client_id', vals.get('client_id', ''))
            if 'client_secret' in vals:
                ICP.set_param('tiendanube_connector.client_secret', vals.get('client_secret', ''))
            if 'access_token' in vals:
                ICP.set_param('tiendanube_connector.access_token', vals.get('access_token', ''))
            if 'refresh_token' in vals:
                ICP.set_param('tiendanube_connector.refresh_token', vals.get('refresh_token', ''))
            if 'store_id' in vals:
                ICP.set_param('tiendanube_connector.store_id', vals.get('store_id', ''))
            if 'api_base_url' in vals:
                ICP.set_param('tiendanube_connector.api_base_url', vals.get('api_base_url', ''))
            if 'stock_location_id' in vals:
                ICP.set_param('tiendanube_connector.stock_location_id', vals.get('stock_location_id', False) or '')
            if 'pricelist_id' in vals:
                ICP.set_param('tiendanube_connector.pricelist_id', vals.get('pricelist_id', False) or '')
            if 'sale_pricelist_id' in vals:
                ICP.set_param('tiendanube_connector.sale_pricelist_id', vals.get('sale_pricelist_id', False) or '')
            if 'fallback_product_id' in vals:
                ICP.set_param('tiendanube_connector.fallback_product_id', vals.get('fallback_product_id', False) or '')
            if 'invoice_journal_id' in vals:
                ICP.set_param('tiendanube_connector.invoice_journal_id', vals.get('invoice_journal_id', False) or '')
            if 'default_income_account_id' in vals:
                ICP.set_param('tiendanube_connector.default_income_account_id', vals.get('default_income_account_id', False) or '')
            
            # Crear webhook automáticamente si hay credenciales válidas y no existe uno
            if (record.access_token and record.store_id and not record.webhook_id):
                # Verificar si las credenciales cambiaron (access_token o store_id)
                if 'access_token' in vals or 'store_id' in vals:
                    try:
                        record._auto_create_webhook()
                    except Exception as e:
                        # No fallar el guardado si falla el webhook, solo log
                        _logger.warning("⚠️ No se pudo crear webhook automáticamente: %s", e)
        
        return result

    @api.depends()
    def _compute_cron_orders_status(self):
        """Estado del cron de sincronización de órdenes (global por base de datos)."""
        try:
            cron = self.env.ref(
                'tiendanube_connector.ir_cron_sync_orders_tiendanube',
                raise_if_not_found=False
            )
            if cron:
                for rec in self:
                    rec.cron_orders_next_run = cron.nextcall
                    rec.cron_orders_last_run = cron.lastcall if hasattr(cron, 'lastcall') else False
                    rec.cron_orders_active = cron.active
            else:
                for rec in self:
                    rec.cron_orders_next_run = False
                    rec.cron_orders_last_run = False
                    rec.cron_orders_active = False
        except Exception:
            for rec in self:
                rec.cron_orders_next_run = False
                rec.cron_orders_last_run = False
                rec.cron_orders_active = False

    def _prepare_create_vals_from_icp(self, vals):
        """Completa vals con ir.config_parameter (un solo dict)."""
        vals = dict(vals)
        ICP = self.env['ir.config_parameter'].sudo()

        if not vals.get('client_id'):
            vals['client_id'] = ICP.get_param('tiendanube_connector.client_id', '')
        if not vals.get('client_secret'):
            vals['client_secret'] = ICP.get_param('tiendanube_connector.client_secret', '')
        if not vals.get('access_token'):
            vals['access_token'] = ICP.get_param('tiendanube_connector.access_token', '')
        if not vals.get('refresh_token'):
            vals['refresh_token'] = ICP.get_param('tiendanube_connector.refresh_token', '')
        if not vals.get('store_id'):
            vals['store_id'] = ICP.get_param('tiendanube_connector.store_id', '')
        if not vals.get('api_base_url'):
            vals['api_base_url'] = ICP.get_param(
                'tiendanube_connector.api_base_url', 'https://api.tiendanube.com/v1'
            )
        if 'stock_location_id' not in vals:
            stock_location_id = ICP.get_param('tiendanube_connector.stock_location_id', '')
            if stock_location_id:
                try:
                    vals['stock_location_id'] = int(stock_location_id)
                except (ValueError, TypeError):
                    pass
        if 'pricelist_id' not in vals:
            pricelist_id = ICP.get_param('tiendanube_connector.pricelist_id', '')
            if pricelist_id:
                try:
                    vals['pricelist_id'] = int(pricelist_id)
                except (ValueError, TypeError):
                    pass
        if 'sale_pricelist_id' not in vals:
            sale_pricelist_id = ICP.get_param('tiendanube_connector.sale_pricelist_id', '')
            if sale_pricelist_id:
                try:
                    vals['sale_pricelist_id'] = int(sale_pricelist_id)
                except (ValueError, TypeError):
                    pass
        if 'fallback_product_id' not in vals:
            fallback_product_id = ICP.get_param('tiendanube_connector.fallback_product_id', '')
            if fallback_product_id:
                try:
                    vals['fallback_product_id'] = int(fallback_product_id)
                except (ValueError, TypeError):
                    pass
        if 'invoice_journal_id' not in vals:
            invoice_journal_id = ICP.get_param('tiendanube_connector.invoice_journal_id', '')
            if invoice_journal_id:
                try:
                    vals['invoice_journal_id'] = int(invoice_journal_id)
                except (ValueError, TypeError):
                    pass
        if 'default_income_account_id' not in vals:
            default_income_account_id = ICP.get_param('tiendanube_connector.default_income_account_id', '')
            if default_income_account_id:
                try:
                    vals['default_income_account_id'] = int(default_income_account_id)
                except (ValueError, TypeError):
                    pass
        return vals

    @api.model_create_multi
    def create(self, vals_list):
        """Al crear, cargar valores desde ir.config_parameter si existen."""
        vals_list = [self._prepare_create_vals_from_icp(v) for v in vals_list]
        return super(TNConfig, self).create(vals_list)

    def action_test_connection(self):
        """Prueba la conexión con TiendaNube haciendo un GET /products?page=1"""
        self.ensure_one()
        
        if not self.access_token:
            raise UserError(_('Debe configurar el Access Token primero'))
        
        if not self.store_id:
            raise UserError(_('Debe configurar el Store ID primero'))
        
        try:
            # Guardar primero los valores
            self.write({})  # Esto sincroniza con ir.config_parameter
            
            sync_service = self.env['tn.sync.service']
            result = sync_service.test_connection()
            
            if result.get('success'):
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': _('Conexión exitosa'),
                        'message': _('La conexión con TiendaNube se estableció correctamente. Credenciales válidas.'),
                        'type': 'success',
                        'sticky': False,
                    }
                }
            else:
                error_msg = result.get('error', 'Desconocido')
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': _('Error en la conexión'),
                        'message': _('No se pudo conectar con TiendaNube: %s') % error_msg,
                        'type': 'danger',
                        'sticky': True,
                    }
                }
        except Exception as e:
            _logger.exception("Error al probar conexión: %s", e)
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Error al probar la conexión'),
                    'message': _('Error: %s') % str(e),
                    'type': 'danger',
                    'sticky': True,
                }
            }

    def action_authorize(self):
        """Inicia el flujo OAuth de TiendaNube para obtener tokens"""
        self.ensure_one()
        
        if not self.client_id or not self.client_secret:
            raise UserError(_('Debe configurar Client ID y Client Secret primero'))
        
        # Guardar primero los valores
        self.write({})  # Esto sincroniza con ir.config_parameter
        
        # Redirigir a la URL de autorización
        return {
            'type': 'ir.actions.act_url',
            'url': '/tiendanube/oauth/authorize',
            'target': 'new',
        }

    def action_refresh_token(self):
        """Refresca el token de acceso"""
        self.ensure_one()
        
        if not self.client_id or not self.client_secret:
            raise UserError(_('Debe configurar Client ID y Client Secret primero'))
        
        # Si no hay refresh_token pero hay access_token, intentar usar el access_token actual
        if not self.refresh_token:
            if self.access_token:
                # Probar si el access_token actual aún es válido
                try:
                    sync_service = self.env['tn.sync.service']
                    result = sync_service.test_connection()
                    if result.get('success'):
                        return {
                            'type': 'ir.actions.client',
                            'tag': 'display_notification',
                            'params': {
                                'title': _('Token válido'),
                                'message': _('El access token actual es válido. No es necesario refrescarlo.'),
                                'type': 'success',
                                'sticky': False,
                            }
                        }
                except:
                    pass
            
            # Si no hay refresh_token, sugerir autorización OAuth
            raise UserError(_(
                'No hay Refresh Token configurado. '
                'Debe autorizar la aplicación primero usando el botón "Autorizar Aplicación" '
                'o configurar manualmente el Refresh Token si ya lo tiene.'
            ))
        
        try:
            # Guardar primero los valores
            self.write({})  # Esto sincroniza con ir.config_parameter
            
            sync_service = self.env['tn.sync.service']
            result = sync_service.refresh_access_token()
            
            if result.get('success'):
                # Actualizar el access token
                self.access_token = result.get('access_token')
                if result.get('refresh_token'):
                    self.refresh_token = result.get('refresh_token')
                
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': _('Token actualizado'),
                        'message': _('El token de acceso se renovó correctamente'),
                        'type': 'success',
                        'sticky': False,
                    }
                }
            else:
                raise UserError(_('Error al refrescar el token: %s') % result.get('error', 'Desconocido'))
        except Exception as e:
            _logger.exception("Error al refrescar token: %s", e)
            raise UserError(_('Error al refrescar el token: %s') % str(e))

    def action_import_publications(self):
        """Importa publicaciones desde TiendaNube usando esta cuenta"""
        self.ensure_one()
        
        # Guardar primero los valores
        self.write({})  # Esto sincroniza con ir.config_parameter
        
        try:
            publication_model = self.env['tn.publication']
            return publication_model.action_import_from_tn(tn_config=self)
        except Exception as e:
            _logger.exception("Error al importar publicaciones: %s", e)
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Error al importar'),
                    'message': _('Error: %s') % str(e),
                    'type': 'danger',
                    'sticky': True,
                }
            }

    def _auto_create_webhook(self):
        """Crea automáticamente el webhook en TiendaNube (llamado desde write)"""
        self.ensure_one()
        
        if not self.access_token or not self.store_id:
            return False
        
        # Verificar si ya existe un webhook configurado
        if self.webhook_id:
            _logger.info("ℹ️ Ya existe un webhook configurado (ID: %s)", self.webhook_id)
            return True
        
        try:
            sync_service = self.env['tn.sync.service']
            
            # Verificar si ya existe un webhook para esta URL usando esta configuración
            existing_webhooks = sync_service.list_webhooks(tn_config=self)
            if existing_webhooks:
                ICP = self.env['ir.config_parameter'].sudo()
                base_url = ICP.get_param('web.base.url', '')
                if base_url:
                    webhook_url = f"{base_url.rstrip('/')}/tiendanube/webhook/order"
                    for wh in existing_webhooks:
                        if isinstance(wh, dict) and wh.get('url') == webhook_url and wh.get('event') == 'order/created':
                            # Ya existe, guardar el ID
                            self.webhook_id = wh.get('id')
                            self.webhook_url = webhook_url
                            _logger.info("✅ Webhook ya existe en TiendaNube (ID: %s), actualizando registro", self.webhook_id)
                            return True
            
            # Obtener URL base de Odoo
            ICP = self.env['ir.config_parameter'].sudo()
            base_url = ICP.get_param('web.base.url', '')
            if not base_url:
                _logger.warning("⚠️ No se puede crear webhook: web.base.url no configurado")
                return False

            webhook_url = f"{base_url.rstrip('/')}/tiendanube/webhook/order"
            
            # Crear webhook para evento order/created usando esta configuración
            result_created = sync_service.create_webhook('order/created', webhook_url, tn_config=self)
            
            # Crear webhook para evento order/updated (para actualizaciones de estado de envío)
            result_updated = sync_service.create_webhook('order/updated', webhook_url, tn_config=self)
            
            if result_created.get('success'):
                self.webhook_id = result_created.get('webhook_id')
                self.webhook_url = webhook_url
                _logger.info("✅ Webhook order/created creado automáticamente en TiendaNube (ID: %s, URL: %s)", 
                           self.webhook_id, webhook_url)
                
                if result_updated.get('success'):
                    _logger.info("✅ Webhook order/updated creado automáticamente en TiendaNube (ID: %s)", 
                               result_updated.get('webhook_id'))
                else:
                    _logger.warning("⚠️ No se pudo crear webhook order/updated: %s", result_updated.get('error'))
                
                return True
            else:
                _logger.warning("⚠️ No se pudo crear webhook automáticamente: %s", result_created.get('error'))
                return False
                
        except Exception as e:
            _logger.exception("❌ Error al crear webhook automáticamente: %s", e)
            return False

    def action_toggle_webhook(self):
        """Activa o pausa el procesamiento de webhooks"""
        self.ensure_one()
        
        new_state = not self.webhook_enabled
        self.webhook_enabled = new_state
        
        if new_state:
            message = _('Webhooks de ventas activados. Las notificaciones se procesarán normalmente.')
            message_type = 'success'
        else:
            message = _('Webhooks de ventas pausados. Las notificaciones seguirán llegando pero no se procesarán. El webhook permanece activo en TiendaNube.')
            message_type = 'warning'
        
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Estado de Webhooks'),
                'message': message,
                'type': message_type,
                'sticky': False,
            }
        }
    
    def action_create_webhook(self):
        """Crea o verifica el webhook en TiendaNube (acción manual)"""
        self.ensure_one()
        
        if not self.access_token or not self.store_id:
            raise UserError(_('Debe configurar Access Token y Store ID primero'))
        
        try:
            # Intentar crear automáticamente
            success = self._auto_create_webhook()
            
            if success:
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': _('Webhook configurado'),
                        'message': _('Webhook configurado correctamente en TiendaNube. URL: %s') % (self.webhook_url or 'N/A'),
                        'type': 'success',
                        'sticky': False,
                    }
                }
            else:
                raise UserError(_('No se pudo crear el webhook. Verifique los logs para más detalles.'))
                
        except Exception as e:
            _logger.exception("Error al crear webhook: %s", e)
            raise UserError(_('Error al crear webhook: %s') % str(e))
    
    def action_sync_all_stocks_and_prices(self):
        """Sincroniza el stock y precio de todas las publicaciones a TiendaNube"""
        self.ensure_one()
        return self.env['tn.publication'].action_sync_all_stocks_and_prices_to_tiendanube()

    def action_sync_all_stocks(self):
        """Sincroniza solo stock de todas las publicaciones a TiendaNube."""
        self.ensure_one()
        return self.env['tn.publication'].action_sync_all_stocks_to_tiendanube()

    def action_sync_all_prices(self):
        """Sincroniza solo precio de todas las publicaciones a TiendaNube."""
        self.ensure_one()
        return self.env['tn.publication'].action_sync_all_prices_to_tiendanube()
    
    def sync_orders_by_polling(self, date_from=None, date_to=None):
        """
        Sincroniza órdenes desde TiendaNube mediante polling (consulta directa a la API).

        Si se pasan date_from y opcionalmente date_to (p. ej. desde el wizard), se usa ese rango
        y no se actualiza last_sync_date. Si no se pasan, se usa last_sync_date o últimas 24 h
        y sí se actualiza last_sync_date (comportamiento para cron / botón antiguo).

        Idempotencia: tn_order_id + company_id (crear o actualizar, nunca duplicar).
        """
        self.ensure_one()

        if not self.access_token or not self.store_id:
            _logger.warning("⚠️ Configuración incompleta (sin access_token o store_id). Omitiendo sincronización.")
            return {
                'success': False,
                'message': 'Configuración incompleta',
                'orders_processed': 0
            }

        try:
            from datetime import datetime, timedelta

            sync_service = self.env['tn.sync.service']
            use_custom_range = date_from is not None

            if use_custom_range:
                if isinstance(date_from, str):
                    try:
                        date_from = datetime.strptime(date_from[:10], '%Y-%m-%d')
                    except Exception:
                        date_from = datetime.now() - timedelta(days=7)
                if date_to is not None and isinstance(date_to, str):
                    try:
                        date_to = datetime.strptime(date_to[:10], '%Y-%m-%d')
                    except Exception:
                        date_to = None
                _logger.info("📅 Sincronizando órdenes por rango: desde %s hasta %s", date_from, date_to or "ahora")
            else:
                hours_back = self._get_order_polling_hours_back()
                max_date_back = datetime.now() - timedelta(hours=hours_back)
                if self.last_sync_date:
                    date_from = max(self.last_sync_date, max_date_back)
                else:
                    date_from = max_date_back
                date_to = None
                _logger.info("📅 Sincronizando órdenes desde: %s (últimas %d h)", date_from, hours_back)

            _logger.info("=" * 80)
            _logger.info("🔄 INICIANDO SINCRONIZACIÓN POR POLLING")
            _logger.info("   Configuración: %s (Store ID: %s)", self.name, self.store_id)
            _logger.info("   Fecha desde: %s | hasta: %s", date_from, date_to or "—")
            _logger.info("=" * 80)

            orders_per_page = min(200, self.polling_orders_per_page or 200)
            orders = sync_service.search_orders_by_date(
                date_from=date_from,
                date_to=date_to,
                tn_config=self,
                limit=orders_per_page
            )
            
            if not orders:
                _logger.info("ℹ️ No se encontraron órdenes nuevas desde %s", date_from)
                return {
                    'success': True,
                    'message': 'No hay órdenes nuevas',
                    'orders_processed': 0
                }
            
            _logger.info("📦 Encontradas %d órdenes para procesar", len(orders))
            
            # Procesar cada orden
            orders_created = 0
            orders_updated = 0
            orders_skipped = 0
            errors = []
            
            for order_data in orders:
                try:
                    order_id = order_data.get('id')
                    if not order_id:
                        _logger.warning("⚠️ Orden sin ID, omitiendo: %s", order_data)
                        orders_skipped += 1
                        continue
                    
                    # Verificar si la orden ya existe (idempotencia) usando la compañía de esta configuración
                    existing_order = self.env['tn.sale.order'].search([
                        ('tn_order_id', '=', order_id),
                        ('company_id', '=', self.company_id.id)
                    ], limit=1)
                    
                    if existing_order:
                        # Orden existe: actualizar
                        _logger.info("🔄 Actualizando orden existente: TN Order ID %s", order_id)
                        try:
                            # Ejecutar en contexto de la compañía de esta configuración (multi-compañía)
                            ctx = {'tn_config_id': self.id, 'allowed_company_ids': [self.company_id.id]}
                            self.env['tn.sale.order'].sudo().with_context(**ctx).create_order_from_webhook(
                                order_data,
                                webhook_secret=None
                            )
                            orders_updated += 1
                            self.env['tn.webhook.notification'].mark_as_processed_for_order(order_id, self.company_id.id)
                        except Exception as e:
                            _logger.exception("❌ Error actualizando orden %s: %s", order_id, e)
                            errors.append(f"Orden {order_id}: {str(e)}")
                    else:
                        # Orden no existe: crear
                        _logger.info("➕ Creando nueva orden: TN Order ID %s", order_id)
                        try:
                            ctx = {'tn_config_id': self.id, 'allowed_company_ids': [self.company_id.id]}
                            self.env['tn.sale.order'].sudo().with_context(**ctx).create_order_from_webhook(
                                order_data,
                                webhook_secret=None
                            )
                            orders_created += 1
                            self.env['tn.webhook.notification'].mark_as_processed_for_order(order_id, self.company_id.id)
                        except Exception as e:
                            _logger.exception("❌ Error creando orden %s: %s", order_id, e)
                            errors.append(f"Orden {order_id}: {str(e)}")
                
                except Exception as e:
                    _logger.exception("❌ Error procesando orden: %s", e)
                    errors.append(f"Error general: {str(e)}")
                    continue
            
            # Actualizar last_sync_date solo cuando no es sincronización por rango (cron / botón directo)
            if not use_custom_range:
                self.last_sync_date = datetime.now()
                _logger.info("   Última sincronización actualizada: %s", self.last_sync_date)

            _logger.info("=" * 80)
            _logger.info("✅ SINCRONIZACIÓN COMPLETADA")
            _logger.info("   Órdenes creadas: %d", orders_created)
            _logger.info("   Órdenes actualizadas: %d", orders_updated)
            _logger.info("   Órdenes omitidas: %d", orders_skipped)
            _logger.info("   Errores: %d", len(errors))
            _logger.info("=" * 80)
            
            return {
                'success': True,
                'message': f'Procesadas {orders_created + orders_updated} órdenes',
                'orders_created': orders_created,
                'orders_updated': orders_updated,
                'orders_skipped': orders_skipped,
                'orders_processed': orders_created + orders_updated,
                'errors': errors if errors else None
            }
            
        except Exception as e:
            _logger.exception("❌ Error en sincronización por polling: %s", e)
            return {
                'success': False,
                'message': str(e),
                'orders_processed': 0
            }
    
    @api.model
    def cron_sync_orders_by_polling(self):
        """
        Método llamado por el cron para sincronizar órdenes mediante polling.
        Procesa todas las configuraciones activas de TiendaNube que tengan polling habilitado.
        """
        _logger.info("=" * 80)
        _logger.info("⏰ CRON: Sincronización de órdenes por polling")
        _logger.info("=" * 80)
        
        # Buscar todas las configuraciones activas con polling habilitado
        configs = self.search([
            ('access_token', '!=', False),
            ('store_id', '!=', False),
            ('polling_enabled', '=', True),  # Solo configuraciones con polling activado
        ])
        
        if not configs:
            _logger.info("ℹ️ No hay configuraciones activas de TiendaNube")
            return
        
        _logger.info("📋 Configuraciones encontradas: %d", len(configs))
        
        total_created = 0
        total_updated = 0
        
        for config in configs:
            try:
                _logger.info("🔄 Procesando configuración: %s (Store ID: %s)", config.name, config.store_id)
                result = config.sync_orders_by_polling()
                
                if result.get('success'):
                    total_created += result.get('orders_created', 0)
                    total_updated += result.get('orders_updated', 0)
                    _logger.info("✅ Configuración procesada: %d creadas, %d actualizadas", 
                               result.get('orders_created', 0), result.get('orders_updated', 0))
                else:
                    _logger.error("❌ Error procesando configuración %s: %s", config.name, result.get('message'))
            
            except Exception as e:
                _logger.exception("❌ Error procesando configuración %s: %s", config.name, e)
                continue
        
        _logger.info("=" * 80)
        _logger.info("✅ CRON COMPLETADO")
        _logger.info("   Total creadas: %d", total_created)
        _logger.info("   Total actualizadas: %d", total_updated)
        _logger.info("=" * 80)

    def action_sync_orders_now(self):
        """Abre el wizard para importar órdenes indicando el rango de fechas."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Importar Órdenes de TiendaNube'),
            'res_model': 'tn.import.orders.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_tn_config_id': self.id,
            }
        }

    def action_auto_link_all_products_by_sku(self):
        """Auto-relaciona productos de Odoo con publicaciones de TiendaNube por SKU"""
        self.ensure_one()
        return self.env['tn.publication'].action_auto_link_all_products_by_sku()


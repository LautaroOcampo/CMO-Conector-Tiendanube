# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError
import hashlib
import hmac
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

    # Credenciales TiendaNube (groups: no exponer vía RPC a usuarios sin rol; la vista también los oculta)
    client_id = fields.Char(
        string='Client ID',
        help='Client ID de la aplicación TiendaNube',
        groups='tiendanube_connector.group_tn_manager',
    )

    client_secret = fields.Char(
        string='Client Secret',
        copy=False,
        groups='tiendanube_connector.group_tn_admin',
        help='Client Secret de la aplicación TiendaNube. TiendaNube firma los webhooks con '
             'HMAC-SHA256 del cuerpo raw usando este valor (cabecera x-linkedstore-hmac-sha256); '
             'debe coincidir con el secret de la app en el panel de desarrolladores.'
    )

    access_token = fields.Char(
        string='Access Token',
        copy=False,
        groups='tiendanube_connector.group_tn_admin',
        help='Access Token de TiendaNube'
    )

    refresh_token = fields.Char(
        string='Refresh Token',
        copy=False,
        groups='tiendanube_connector.group_tn_admin',
        help='Refresh Token de TiendaNube'
    )
    
    store_id = fields.Char(
        string='Store ID',
        help='ID de la tienda en TiendaNube'
    )

    tn_store_country_code = fields.Char(
        string='País de la tienda (ISO)',
        readonly=True,
        copy=False,
        help='Código ISO 3166-1 de dos letras según GET /store de TiendaNube (solo lectura).',
    )
    tn_store_country_id = fields.Many2one(
        'res.country',
        string='País de la tienda (Odoo)',
        readonly=True,
        copy=False,
        help='País de la tienda enlazado a res.country; se rellena al sincronizar con la API.',
    )
    tn_store_main_currency = fields.Char(
        string='Moneda principal (ISO)',
        readonly=True,
        copy=False,
        help='Moneda principal de la tienda (ISO 4217) según TiendaNube; informativo.',
    )
    tn_store_metadata_synced_at = fields.Datetime(
        string='Última lectura de datos de tienda',
        readonly=True,
        copy=False,
        help='Última vez que se consultó GET /store para país y moneda.',
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

    oauth_role = fields.Selection(
        [
            ('direct', 'Directo (callback en este Odoo)'),
            ('hub', 'Hub OAuth (callback central)'),
            ('tenant', 'Cliente (autoriza vía hub)'),
        ],
        string='Modo OAuth',
        default='direct',
        required=True,
    )
    oauth_hub_url = fields.Char()
    oauth_tenant_code = fields.Char(copy=False)
    oauth_shared_secret = fields.Char(
        copy=False,
        groups='tiendanube_connector.group_tn_admin',
    )
    oauth_callback_uri = fields.Char(
        string='Redirect URI (app TiendaNube)',
        compute='_compute_oauth_callback_uri',
    )
    oauth_tenant_ids = fields.One2many(
        'tn.oauth.tenant',
        'config_id',
        string='Bases conectadas',
    )
    oauth_via_hub = fields.Boolean(compute='_compute_oauth_mode')
    oauth_is_hub = fields.Boolean(compute='_compute_oauth_mode')

    @api.depends('oauth_role')
    def _compute_oauth_callback_uri(self):
        base = (self.env['ir.config_parameter'].sudo().get_param('web.base.url') or '').rstrip('/')
        for rec in self:
            rec.oauth_callback_uri = (
                '%s/tiendanube/oauth/callback' % base if base else '/tiendanube/oauth/callback'
            )

    @api.depends()
    def _compute_oauth_mode(self):
        via = self._oauth_via_hub_enabled()
        is_hub = self._oauth_this_is_hub()
        for rec in self:
            rec.oauth_via_hub = via
            rec.oauth_is_hub = is_hub
    
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
    auto_invoice_with_fallback_product = fields.Boolean(
        string='Facturar automáticamente con producto fallback',
        default=False,
        help=(
            'Si está activo, las líneas sin producto Odoo identificado usan el producto fallback, '
            'confirman la orden y facturan igual (requiere «Facturar Automáticamente»). '
            'En el PDF de la factura se muestra el nombre del producto en TiendaNube. '
            'Si está desactivado, la orden queda en borrador para revisión manual.'
        ),
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
        domain=[('active', '=', True)],
        help='Cuenta contable de ingresos a usar en líneas de factura de ventas TiendaNube (reemplaza la cuenta por defecto del producto/categoría).'
    )
    
    default_tax_id = fields.Many2one(
        'account.tax',
        string='Impuesto por Defecto',
        domain=[('type_tax_use', '=', 'sale')],
        help='Impuesto que se aplicará por defecto a las líneas de las órdenes de venta creadas desde TiendaNube.'
    )
    
    auto_create_sale_order = fields.Boolean(
        string='Crear Orden de Venta Automáticamente',
        default=True,
        help=(
            'Si está activado, al importar la venta TN se crea y confirma la orden de venta en Odoo. '
            'Si está desactivado, solo se guarda tn.sale.order y podés usar el botón '
            '«Crear Orden de Venta en Odoo» en cada venta.'
        ),
    )
    auto_invoice = fields.Boolean(
        string='Facturar Automáticamente',
        default=False,
        help=(
            'Requiere «Crear Orden de Venta Automáticamente». Si está activado, se creará y publicará '
            'automáticamente la factura al crear la orden de venta desde TiendaNube.'
        ),
    )

    consumidor_final_partner_id = fields.Many2one(
        'res.partner',
        string='Contacto Consumidor Final',
        domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]",
        help=(
            'Contacto usado en todas las órdenes de venta y facturas de TiendaNube '
            '(Consumidor Final). Obligatorio para importar ventas a Odoo.'
        ),
    )
    create_order_contacts = fields.Boolean(
        string='Crear contacto del comprador',
        default=False,
        help=(
            'Si está activo, además del Consumidor Final en la venta/factura, se busca o crea '
            'un contacto en Odoo con los datos del comprador (ID TN, email, DNI/CUIT, teléfono, '
            'dirección, etc.). Ese contacto queda vinculado en la venta TN como «Comprador»; '
            'no se usa para facturar.'
        ),
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
    webhook_expected_url = fields.Char(
        string='URL esperada (Odoo)',
        compute='_compute_webhook_urls',
        help='URL que Odoo espera según web.base.url actual. Debe coincidir con la registrada en TiendaNube.',
    )
    webhook_url_mismatch = fields.Boolean(
        string='URL webhook desincronizada',
        compute='_compute_webhook_urls',
    )

    webhook_enabled = fields.Boolean(
        string='Procesar Webhooks de Ventas',
        default=True,
        help='Si está desactivado, los webhooks de ventas seguirán llegando pero no se procesarán. El webhook permanece activo en TiendaNube.'
    )
    webhook_last_received_at = fields.Datetime(
        string='Último webhook recibido',
        readonly=True,
        copy=False,
        help='Actualizado cada vez que TiendaNube llama a /tiendanube/webhook/order.',
    )
    webhook_last_status = fields.Selection(
        [
            ('ok', 'OK — venta importada'),
            ('received', 'Recibido — guardado'),
            ('rejected', 'Rechazado'),
            ('error', 'Error al procesar'),
            ('ping', 'Ping GET'),
        ],
        string='Estado último webhook',
        readonly=True,
        copy=False,
    )
    webhook_last_summary = fields.Char(
        string='Resumen último webhook',
        readonly=True,
        copy=False,
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
    
    import_publications_active = fields.Boolean(
        string='Importación de publicaciones en curso',
        default=False,
        copy=False,
        help='La importación masiva corre en segundo plano (cron). Cada lote confirma en base de datos.',
    )
    import_publications_page = fields.Integer(
        string='Página actual de importación',
        default=1,
        copy=False,
    )
    import_publications_batch_size = fields.Integer(
        string='Productos por lote de importación',
        default=25,
        help='Productos TN procesados por ejecución del cron (5–50). Valores bajos evitan timeouts en Odoo.sh.',
    )
    import_download_images = fields.Boolean(
        string='Descargar imágenes al importar',
        default=False,
        help='Desactivado acelera la importación masiva. Solo guarda las URLs; el binario se puede traer después.',
    )
    scan_new_publications_enabled = fields.Boolean(
        string='Detectar publicaciones nuevas (cron diario)',
        default=False,
        help=(
            'Una vez al día lista el catálogo TN (GET /products paginado) e importa solo productos '
            'cuyo ID aún no existe en Odoo. No corre mientras haya una importación masiva activa.'
        ),
    )
    scan_new_publications_import_limit = fields.Integer(
        string='Máx. productos nuevos por día',
        default=50,
        help='Tope de productos TN nuevos a importar por ejecución del cron diario (1–200). '
             'Si hay más pendientes, se procesan en días siguientes.',
    )
    scan_new_publications_last_run = fields.Datetime(
        string='Último scan de novedades',
        readonly=True,
        copy=False,
    )
    scan_new_publications_last_imported = fields.Integer(
        string='Importadas en último scan',
        readonly=True,
        copy=False,
    )

    auto_sync_stock_on_odoo_change = fields.Boolean(
        string='Sincronizar Stock Automáticamente desde Odoo',
        default=False,
        help='Si está activado, cuando se modifique el stock en Odoo (por ventas, compras, ajustes manuales, etc.), se sincronizará automáticamente el stock a TiendaNube. El stock se copia directamente (valor absoluto), no se suma ni resta.'
    )

    auto_sync_price_on_odoo_change = fields.Boolean(
        string='Sincronizar Precio Automáticamente desde Odoo',
        default=False,
        help='Deshabilitado: el precio en TiendaNube solo se actualiza desde la publicación («Actualizar valores»).',
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

    def _tn_credentials(self):
        """Mismo registro en sudo: leer tokens/secret sin depender del grupo del usuario (servicios, cron, OAuth)."""
        self.ensure_one()
        return self.sudo()

    OAUTH_HUB_URL_KEY = 'tiendanube_connector.oauth_hub_url'
    OAUTH_HUB_SECRET_KEY = 'tiendanube_connector.oauth_shared_secret'

    @api.model
    def _oauth_hub_url(self):
        return (self.env['ir.config_parameter'].sudo().get_param(self.OAUTH_HUB_URL_KEY) or '').strip().rstrip('/')

    @api.model
    def _oauth_hub_secret(self):
        return (self.env['ir.config_parameter'].sudo().get_param(self.OAUTH_HUB_SECRET_KEY) or '').strip()

    @api.model
    def _oauth_same_host(self, url_a, url_b):
        if not url_a or not url_b:
            return False
        from urllib.parse import urlparse
        a, b = urlparse(url_a), urlparse(url_b)
        return (a.netloc or '').lower() == (b.netloc or '').lower()

    @api.model
    def _oauth_via_hub_enabled(self):
        return bool(self._oauth_hub_url() and self._oauth_hub_secret())

    @api.model
    def _oauth_this_is_hub(self):
        if not self._oauth_via_hub_enabled():
            return False
        base = (self.env['ir.config_parameter'].sudo().get_param('web.base.url') or '').rstrip('/')
        return self._oauth_same_host(self._oauth_hub_url(), base)

    def _verify_oauth_install_signature(self, raw_body, signature):
        """HMAC-SHA256 del body JSON con el secreto del hub (parámetro de sistema)."""
        secret = (self._oauth_hub_secret() or '').encode('utf-8')
        if not secret or not raw_body or not signature:
            return False
        if isinstance(raw_body, str):
            raw_body = raw_body.encode('utf-8')
        expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
        try:
            return hmac.compare_digest(expected, str(signature))
        except (TypeError, ValueError):
            return False

    def record_webhook_health(self, status, summary):
        """Registra en la config y en logs si el webhook está llegando y cómo se procesó."""
        self.ensure_one()
        summary = (summary or '')[:255]
        self.sudo().write({
            'webhook_last_received_at': fields.Datetime.now(),
            'webhook_last_status': status,
            'webhook_last_summary': summary,
        })
        _logger.info(
            '[TN WEBHOOK][HEALTH] cuenta=%s (id=%s) status=%s %s',
            self.name,
            self.id,
            status,
            summary,
        )

    @api.model
    def verify_linkedstore_webhook_hmac(self, raw_body, hmac_header, client_secret):
        """Comprueba la cabecera x-linkedstore-hmac-sha256 (Nuvemshop / TiendaNube API).

        Documentación: el digest es ``hash_hmac('sha256', raw_body, client_secret)`` en hex
        (mismo valor que ``Client Secret`` de la aplicación).
        """
        if not raw_body or not hmac_header or not client_secret:
            return False
        try:
            key = str(client_secret).encode('utf-8')
            expected = hmac.new(key, raw_body, hashlib.sha256).hexdigest()
            received = str(hmac_header).strip().lower()
            return hmac.compare_digest(expected, received)
        except Exception:
            return False

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

    @api.constrains('auto_invoice', 'auto_create_sale_order')
    def _check_auto_invoice_requires_sale_order(self):
        for rec in self:
            if rec.auto_invoice and not rec.auto_create_sale_order:
                raise ValidationError(
                    _('No puede activar facturación automática sin crear orden de venta automáticamente.')
                )

    @api.onchange('auto_create_sale_order')
    def _onchange_auto_create_sale_order(self):
        if not self.auto_create_sale_order:
            self.auto_invoice = False

    def write(self, vals):
        """Al guardar, también actualizar ir.config_parameter y crear webhook automáticamente si corresponde"""
        vals = dict(vals)
        if vals.get('auto_create_sale_order') is False:
            vals['auto_invoice'] = False
        result = super(TNConfig, self).write(vals)
        skip_meta = self.env.context.get('skip_tn_store_metadata_sync')

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
            if (record._tn_credentials().access_token and record.store_id and not record.webhook_id):
                # Verificar si las credenciales cambiaron (access_token o store_id)
                if 'access_token' in vals or 'store_id' in vals:
                    try:
                        record._auto_create_webhook()
                    except Exception as e:
                        # No fallar el guardado si falla el webhook, solo log
                        _logger.warning("⚠️ No se pudo crear webhook automáticamente: %s", e)

            if not skip_meta and record._tn_credentials().access_token and record.store_id:
                if {'access_token', 'store_id', 'api_base_url'} & set(vals.keys()):
                    try:
                        record._sync_store_metadata_from_api()
                    except Exception as e:
                        _logger.warning(
                            "⚠️ No se pudo actualizar país/moneda de la tienda (GET /store): %s",
                            e,
                        )

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
        records = super(TNConfig, self).create(vals_list)
        for rec in records:
            if rec._tn_credentials().access_token and rec.store_id:
                try:
                    rec._sync_store_metadata_from_api()
                except Exception as e:
                    _logger.warning(
                        "⚠️ No se pudo actualizar país/moneda al crear tn.config: %s",
                        e,
                    )
        return records

    def _sync_store_metadata_from_api(self):
        """Lee GET /store y guarda país y moneda (campos de solo lectura en UI)."""
        self.ensure_one()
        if not self._tn_credentials().access_token or not self.store_id:
            return False
        sync = self.env['tn.sync.service'].sudo()
        try:
            payload = sync._make_request(
                'GET',
                '/store',
                params={'fields': 'country,main_currency'},
                retry=False,
                tn_config=self,
            )
        except Exception as e:
            _logger.warning("GET /store falló (config id=%s): %s", self.id, e)
            return False
        if not isinstance(payload, dict):
            return False
        code = (payload.get('country') or '').strip().upper()[:2]
        country = False
        if code and len(code) == 2:
            country = self.env['res.country'].search([('code', '=', code)], limit=1)
        main_curr = (payload.get('main_currency') or '').strip() or False
        self.with_context(skip_tn_store_metadata_sync=True).write({
            'tn_store_country_code': code or False,
            'tn_store_country_id': country.id if country else False,
            'tn_store_main_currency': main_curr,
            'tn_store_metadata_synced_at': fields.Datetime.now(),
        })
        _logger.info(
            "🌍 Datos de tienda TN actualizados: país=%s, moneda=%s (config %s)",
            code or '-', main_curr or '-', self.id,
        )
        return True

    def get_res_country_for_tn_store(self):
        """País Odoo asociado a la tienda (API); vacío si aún no sincronizó o no hay match."""
        self.ensure_one()
        if self.tn_store_country_id:
            return self.tn_store_country_id
        code = (self.tn_store_country_code or '').strip().upper()
        if code and len(code) == 2:
            return self.env['res.country'].search([('code', '=', code)], limit=1)
        return self.env['res.country'].browse()

    def get_fallback_country_for_partners(self):
        """País para nuevos contactos desde ventas TN: tienda API o Argentina como respaldo."""
        self.ensure_one()
        c = self.get_res_country_for_tn_store()
        if c:
            return c
        return self.env['res.country'].search([('code', '=', 'AR')], limit=1)

    def action_refresh_store_country_metadata(self):
        """Botón: vuelve a leer GET /store (país y moneda)."""
        self.ensure_one()
        if not self._tn_credentials().access_token or not self.store_id:
            raise UserError(_('Configure Access Token y Store ID para consultar la tienda.'))
        if not self._sync_store_metadata_from_api():
            raise UserError(_('No se pudo obtener la información de la tienda. Revise los logs.'))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Tienda actualizada'),
                'message': _('País y moneda se leyeron de nuevo desde TiendaNube.'),
                'type': 'success',
                'sticky': False,
            },
        }

    def action_test_connection(self):
        """Prueba la conexión con TiendaNube haciendo un GET /products?page=1"""
        self.ensure_one()
        
        if not self._tn_credentials().access_token:
            raise UserError(_('Debe configurar el Access Token primero'))
        
        if not self.store_id:
            raise UserError(_('Debe configurar el Store ID primero'))
        
        try:
            # Guardar primero los valores
            self.write({})  # Esto sincroniza con ir.config_parameter
            
            sync_service = self.env['tn.sync.service']
            result = sync_service.test_connection()
            
            if result.get('success'):
                try:
                    self._sync_store_metadata_from_api()
                except Exception as e:
                    _logger.warning("Conexión OK pero GET /store falló: %s", e)
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
        cred = self._tn_credentials()
        uses_hub = cred._oauth_via_hub_enabled() and not cred._oauth_this_is_hub()
        has_local_app = bool((cred.client_id or '').strip() and (cred.client_secret or '').strip())
        if not uses_hub and not has_local_app:
            raise UserError(_(
                'Configurá Client ID y Client Secret (modo directo), '
                'o los parámetros oauth_hub_url y oauth_shared_secret (modo hub).'
            ))

        self.write({})
        return {
            'type': 'ir.actions.act_url',
            'url': '/tiendanube/oauth/authorize',
            'target': 'new',
        }

    def action_refresh_token(self):
        """Refresca el token de acceso"""
        self.ensure_one()
        cred = self._tn_credentials()
        if not cred.client_id or not cred.client_secret:
            raise UserError(_('Debe configurar Client ID y Client Secret primero'))
        
        # Si no hay refresh_token pero hay access_token, intentar usar el access_token actual
        if not cred.refresh_token:
            if cred.access_token:
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
                write_vals = {}
                if result.get('access_token'):
                    write_vals['access_token'] = result.get('access_token')
                if result.get('refresh_token'):
                    write_vals['refresh_token'] = result.get('refresh_token')
                if write_vals:
                    self.sudo().write(write_vals)
                
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
        """Encola importación masiva en segundo plano (cron, con commit por producto)."""
        self.ensure_one()
        cred = self._tn_credentials()
        if not cred.access_token or not self.store_id:
            raise UserError(_('Configure Access Token y Store ID antes de importar.'))

        if self.import_publications_active:
            raise UserError(_(
                'Ya hay una importación en curso (página %d). '
                'Espere a que termine o use «Detener importación».'
            ) % (self.import_publications_page or 1))

        batch_size = self.import_publications_batch_size or 25
        if batch_size < 5 or batch_size > 50:
            raise UserError(_('Productos por lote debe estar entre 5 y 50.'))

        self.write({
            'import_publications_active': True,
            'import_publications_page': 1,
        })

        cron = self.env.ref(
            'tiendanube_connector.ir_cron_import_publications_tiendanube',
            raise_if_not_found=False,
        )
        if cron:
            cron._trigger()

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Importación iniciada'),
                'message': _(
                    'La importación corre en segundo plano (~%(batch)d productos por minuto). '
                    'El progreso queda guardado en logs y en Publicaciones TiendaNube. '
                    'Imágenes: %(images)s.'
                ) % {
                    'batch': batch_size,
                    'images': _('sí') if self.import_download_images else _('no (solo URLs)'),
                },
                'type': 'success',
                'sticky': True,
            },
        }

    def action_stop_import_publications(self):
        """Detiene la importación masiva en curso."""
        self.ensure_one()
        if not self.import_publications_active:
            raise UserError(_('No hay importación en curso.'))
        page = self.import_publications_page or 1
        self.write({
            'import_publications_active': False,
            'import_publications_page': 1,
        })
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Importación detenida'),
                'message': _(
                    'Se detuvo en la página %d. Los productos ya importados permanecen en Odoo. '
                    'Puede reanudar con «Importar Publicaciones» (omitirá los existentes).'
                ) % page,
                'type': 'warning',
                'sticky': False,
            },
        }

    @api.model
    def cron_import_publications_batch(self):
        """Cron: procesa un lote de importación por cada cuenta activa."""
        configs = self.search([
            ('import_publications_active', '=', True),
            ('store_id', '!=', False),
        ])
        for config in configs:
            try:
                config._run_import_publications_batch()
            except Exception as e:
                _logger.exception(
                    "❌ Cron importación publicaciones falló (%s): %s",
                    config.display_name,
                    e,
                )

    def _run_import_publications_batch(self):
        """Un lote de importación con commit por producto (sobrevive al límite de 900s del HTTP)."""
        self.ensure_one()
        cred = self._tn_credentials()
        if not cred.access_token or not self.store_id:
            self.import_publications_active = False
            return

        page = self.import_publications_page or 1
        batch_size = max(5, min(self.import_publications_batch_size or 25, 50))
        sync_service = self.env['tn.sync.service']

        result = sync_service.import_publications_batch(
            page=page,
            per_page=batch_size,
            tn_config=self,
            skip_existing=True,
            download_images=self.import_download_images,
            commit_after_each=True,
        )

        imported = result.get('imported_count', 0)
        skipped = result.get('skipped_count', 0)
        products_in_page = result.get('products_in_page', 0)
        is_last_page = result.get('last_page', True)

        _logger.info(
            "📥 Importación TN [%s] lote página %d: %d importados, %d omitidos, %d en página",
            self.name,
            page,
            imported,
            skipped,
            products_in_page,
        )

        if is_last_page or products_in_page == 0:
            self.write({
                'import_publications_active': False,
                'import_publications_page': 1,
            })
            _logger.info("📥 Importación TN [%s]: catálogo completado.", self.name)
        else:
            self.write({'import_publications_page': page + 1})

    def _run_scan_new_publications_daily(self):
        """
        Cron diario: scan del catálogo TN + importación de productos nuevos solamente.
        Reutiliza _create_publication_from_tn_data (importa todos los productos nuevos).
        """
        self.ensure_one()
        cred = self._tn_credentials()
        if not cred.access_token or not self.store_id:
            _logger.info(
                '🔍 Scan novedades TN [%s]: omitido (sin token o store_id)',
                self.name,
            )
            return
        if self.import_publications_active:
            _logger.info(
                '🔍 Scan novedades TN [%s]: omitido (importación masiva en curso)',
                self.name,
            )
            return
        if not self.scan_new_publications_enabled:
            return

        import_limit = max(1, min(int(self.scan_new_publications_import_limit or 50), 200))
        sync_service = self.env['tn.sync.service']
        download_images = bool(self.import_download_images)

        new_ids = sync_service.discover_new_tn_product_ids(tn_config=self)
        imported_total = 0
        skipped_total = 0
        error_total = 0

        if new_ids:
            batch = new_ids[:import_limit]
            if len(new_ids) > import_limit:
                _logger.info(
                    '🔍 Scan novedades TN [%s]: importando %d de %d nuevos hoy (límite diario %d)',
                    self.name,
                    len(batch),
                    len(new_ids),
                    import_limit,
                )
            for index, tn_product_id in enumerate(batch, start=1):
                _logger.info(
                    '🔍 Scan novedades TN [%s]: nuevo %d/%d — TN product %s',
                    self.name,
                    index,
                    len(batch),
                    tn_product_id,
                )
                try:
                    result = sync_service.import_single_tn_product_by_id(
                        tn_product_id,
                        tn_config=self,
                        download_images=download_images,
                    )
                    if result.get('imported'):
                        imported_total += result.get('imported_count', 0) or 1
                    else:
                        skipped_total += 1
                    error_total += len(result.get('errors') or [])
                    for err in result.get('errors') or []:
                        _logger.error('❌ Scan novedades TN: %s', err)
                    self.env.cr.commit()
                except Exception as e:
                    error_total += 1
                    _logger.exception(
                        '❌ Scan novedades TN [%s]: error importando producto %s: %s',
                        self.name,
                        tn_product_id,
                        e,
                    )
                    self.env.cr.rollback()

        self.write({
            'scan_new_publications_last_run': fields.Datetime.now(),
            'scan_new_publications_last_imported': imported_total,
        })
        _logger.info(
            '🔍 Scan novedades TN [%s]: fin — %d nuevos detectados, %d publicaciones creadas, '
            '%d omitidos (SKU/existente), %d errores',
            self.name,
            len(new_ids),
            imported_total,
            skipped_total,
            error_total,
        )

    @api.model
    def cron_scan_new_tn_publications(self):
        """Cron diario: detecta e importa publicaciones TN que aún no están en Odoo."""
        configs = self.search([
            ('access_token', '!=', False),
            ('store_id', '!=', False),
            ('scan_new_publications_enabled', '=', True),
            ('import_publications_active', '=', False),
        ])
        for config in configs:
            try:
                config._run_scan_new_publications_daily()
            except Exception as e:
                _logger.exception(
                    '❌ Cron scan novedades TN falló (%s): %s',
                    config.display_name,
                    e,
                )

    def _get_expected_webhook_url(self):
        """URL pública que TiendaNube debe llamar según web.base.url."""
        self.ensure_one()
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '').strip().rstrip('/')
        if not base_url:
            return False
        return f'{base_url}/tiendanube/webhook/order'

    @api.model
    def _normalize_webhook_url(self, url):
        if not url:
            return ''
        return str(url).strip().rstrip('/').lower().replace('http://', 'https://')

    @api.depends('webhook_url')
    def _compute_webhook_urls(self):
        for rec in self:
            expected = rec._get_expected_webhook_url() or ''
            rec.webhook_expected_url = expected
            if expected and rec.webhook_url:
                rec.webhook_url_mismatch = (
                    rec._normalize_webhook_url(rec.webhook_url)
                    != rec._normalize_webhook_url(expected)
                )
            else:
                rec.webhook_url_mismatch = bool(expected and not rec.webhook_url)

    def _sync_webhook_with_tiendanube(self, force_recreate=False):
        """
        Compara webhooks en TiendaNube con la URL esperada de Odoo.
        Crea o reemplaza order/created y order/updated si faltan o apuntan a otra URL.
        """
        self.ensure_one()
        if not self._tn_credentials().access_token or not self.store_id:
            return {'ok': False, 'error': _('Access Token y Store ID requeridos.')}

        expected = self._get_expected_webhook_url()
        if not expected:
            return {'ok': False, 'error': _('Configure web.base.url en Ajustes → Técnico → Parámetros del sistema.')}

        sync_service = self.env['tn.sync.service']
        existing = sync_service.list_webhooks(tn_config=self) or []
        expected_norm = self._normalize_webhook_url(expected)
        events_needed = ('order/created', 'order/updated')

        _logger.info(
            '[TN WEBHOOK] verificación cuenta=%s (id=%s) store=%s url_esperada=%s webhooks_en_TN=%s',
            self.name,
            self.id,
            self.store_id,
            expected,
            [
                (w.get('id'), w.get('event'), w.get('url'))
                for w in existing
                if isinstance(w, dict)
            ],
        )

        matched = {}
        to_delete = []
        for wh in existing:
            if not isinstance(wh, dict):
                continue
            event = wh.get('event')
            wh_id = wh.get('id')
            if event not in events_needed or not wh_id:
                continue
            url_norm = self._normalize_webhook_url(wh.get('url'))
            if url_norm == expected_norm:
                matched[event] = wh
            else:
                to_delete.append(wh)

        missing = [ev for ev in events_needed if ev not in matched]
        needs_work = force_recreate or missing or to_delete

        if not needs_work:
            created_wh = matched['order/created']
            self.write({
                'webhook_id': created_wh.get('id'),
                'webhook_url': created_wh.get('url') or expected,
            })
            _logger.info(
                '[TN WEBHOOK] OK cuenta=%s webhook_id=%s url=%s',
                self.name,
                self.webhook_id,
                self.webhook_url,
            )
            return {
                'ok': True,
                'action': 'verified',
                'expected_url': expected,
                'existing': existing,
            }

        if to_delete or force_recreate:
            delete_ids = set()
            for wh in to_delete:
                delete_ids.add(wh.get('id'))
            if force_recreate:
                for wh in matched.values():
                    delete_ids.add(wh.get('id'))
            for wh_id in delete_ids:
                if wh_id:
                    sync_service.delete_webhook(wh_id, tn_config=self)

        result_created = sync_service.create_webhook('order/created', expected, tn_config=self)
        result_updated = sync_service.create_webhook('order/updated', expected, tn_config=self)

        if not result_created.get('success'):
            return {
                'ok': False,
                'error': result_created.get('error') or _('No se pudo crear webhook order/created'),
                'expected_url': expected,
                'existing': existing,
            }

        self.write({
            'webhook_id': result_created.get('webhook_id'),
            'webhook_url': expected,
        })
        if not result_updated.get('success'):
            _logger.warning(
                '⚠️ Webhook order/updated no creado para %s: %s',
                self.name,
                result_updated.get('error'),
            )

        _logger.info(
            '[TN WEBHOOK] recreado cuenta=%s webhook_id=%s url=%s (antes id=%s)',
            self.name,
            self.webhook_id,
            expected,
            to_delete,
        )
        return {
            'ok': True,
            'action': 'recreated',
            'expected_url': expected,
            'existing': sync_service.list_webhooks(tn_config=self) or [],
        }

    def _auto_create_webhook(self):
        """Crea o sincroniza el webhook en TiendaNube (llamado desde write)."""
        self.ensure_one()
        result = self._sync_webhook_with_tiendanube(force_recreate=False)
        return result.get('ok', False)

    def action_verify_webhook(self):
        """Lista webhooks en TN, compara URL y corrige si apunta a otro host."""
        self.ensure_one()
        result = self._sync_webhook_with_tiendanube(force_recreate=False)
        if not result.get('ok'):
            raise UserError(result.get('error') or _('No se pudo verificar el webhook.'))

        expected = result.get('expected_url') or self._get_expected_webhook_url()
        lines = []
        for wh in result.get('existing') or []:
            if not isinstance(wh, dict):
                continue
            marker = '✓' if self._normalize_webhook_url(wh.get('url')) == self._normalize_webhook_url(expected) else '✗'
            lines.append(f"{marker} ID {wh.get('id')}: {wh.get('event')} → {wh.get('url')}")

        body = _('URL que Odoo espera:\n%s\n\nWebhooks en TiendaNube:\n%s') % (
            expected or 'N/A',
            '\n'.join(lines) if lines else _('(ninguno)'),
        )
        if result.get('action') == 'recreated':
            body += _('\n\nSe recreó el webhook para que apunte a la URL correcta.')
        elif self.webhook_url_mismatch:
            body += _('\n\n⚠ La URL guardada en Odoo no coincide con web.base.url. Use «Sincronizar webhook».')

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Webhook verificado'),
                'message': body,
                'type': 'success' if result.get('action') == 'verified' else 'warning',
                'sticky': True,
            },
        }

    def action_recreate_webhook(self):
        """Elimina y vuelve a registrar webhooks en TiendaNube con la URL actual de Odoo."""
        self.ensure_one()
        result = self._sync_webhook_with_tiendanube(force_recreate=True)
        if not result.get('ok'):
            raise UserError(result.get('error') or _('No se pudo recrear el webhook.'))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Webhook sincronizado'),
                'message': _('Webhooks recreados en TiendaNube apuntando a:\n%s') % (result.get('expected_url') or ''),
                'type': 'success',
                'sticky': False,
            },
        }

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
        """Verifica y sincroniza el webhook en TiendaNube con la URL actual de Odoo."""
        self.ensure_one()
        if not self._tn_credentials().access_token or not self.store_id:
            raise UserError(_('Debe configurar Access Token y Store ID primero'))
        return self.action_verify_webhook()
    
    def action_sync_all_stocks_and_prices(self):
        """Compatibilidad: ya no sincroniza precios a TN; solo stock."""
        self.ensure_one()
        return self.env['tn.publication'].action_sync_all_stocks_to_tiendanube()

    def action_sync_all_stocks(self):
        """Sincroniza solo stock de todas las publicaciones a TiendaNube."""
        self.ensure_one()
        return self.env['tn.publication'].action_sync_all_stocks_to_tiendanube()

    def _tn_get_stock_sync_publications(self):
        """Publicaciones de esta cuenta que recibirían stock desde Odoo."""
        self.ensure_one()
        publications = self.env['tn.publication'].search([
            ('tn_config_id', '=', self.id),
            ('odoo_product_id', '!=', False),
            ('tn_product_id', '!=', False),
            ('active', '=', True),
        ])
        return publications

    def action_view_stock_sync_publications(self):
        """
        Vista previa (solo lectura) de las publicaciones que recibirían stock.

        Mismo criterio que «Sincronizar Todos los Stocks», el cron y el sync
        automático: publicada en TN + producto Odoo relacionado.
        No envía nada a TiendaNube.
        """
        self.ensure_one()
        publications = self._tn_get_stock_sync_publications()
        name = _('Sincronizarían stock: %d publicaciones') % len(publications)
        return {
            'type': 'ir.actions.act_window',
            'name': name,
            'res_model': 'tn.publication',
            'view_mode': 'list,form',
            'domain': [('id', 'in', publications.ids)],
            'context': {'create': False},
        }
    
    def sync_orders_by_polling(self, date_from=None, date_to=None):
        """
        Sincroniza órdenes desde TiendaNube mediante polling (consulta directa a la API).

        Si se pasan date_from y opcionalmente date_to (p. ej. desde el wizard), se usa ese rango
        y no se actualiza last_sync_date. Si no se pasan, se usa last_sync_date o últimas 24 h
        y sí se actualiza last_sync_date (comportamiento para cron / botón antiguo).

        Idempotencia: tn_order_id + company_id (crear o actualizar, nunca duplicar).
        """
        self.ensure_one()

        if not self._tn_credentials().access_token or not self.store_id:
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
        """Abre el wizard: importación por últimas N horas o por rango de fechas, y reintento de webhooks pendientes."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Importar ventas / reintento webhook'),
            'res_model': 'tn.import.orders.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_tn_config_id': self.id,
                'default_import_mode': 'hours',
                'default_hours_back': 12,
                'default_retry_webhook_notifications': True,
            },
        }

    def retry_pending_webhook_notifications(self, limit=300):
        """
        Reprocesa notificaciones de webhook pendientes o fallidas para esta cuenta:
        obtiene la orden en la API y ejecuta el mismo flujo que el webhook (create_order_from_webhook).
        """
        self.ensure_one()
        if not self._tn_credentials().access_token or not self.store_id:
            raise UserError(_('Configure Access Token y Store ID antes de reintentar webhooks.'))

        Notification = self.env['tn.webhook.notification'].sudo()
        sync_service = self.env['tn.sync.service'].sudo()
        domain = [
            ('company_id', '=', self.company_id.id),
            ('tn_config_id', '=', self.id),
            ('status', 'in', ('pending', 'failed')),
        ]
        notifications = Notification.search(domain, order='date_received asc', limit=limit)

        processed = 0
        errors = 0
        ctx = {'tn_config_id': self.id, 'allowed_company_ids': [self.company_id.id]}
        SaleTN = self.env['tn.sale.order'].sudo().with_context(**ctx)

        for notif in notifications:
            try:
                order_data = sync_service._get_order_by_id(notif.order_id, tn_config=self)
                if not order_data:
                    notif.write({'status': 'failed'})
                    errors += 1
                    _logger.warning(
                        "Reintento webhook: sin datos de API para orden TN %s (notificación %s)",
                        notif.order_id, notif.id,
                    )
                    continue
                SaleTN.create_order_from_webhook(order_data, webhook_secret=None)
                Notification.mark_as_processed_for_order(notif.order_id, self.company_id.id)
                notif.write({'status': 'processed'})
                processed += 1
            except Exception:
                errors += 1
                notif.write({'status': 'failed'})
                _logger.exception(
                    "Reintento webhook: error procesando orden TN %s (notificación %s)",
                    notif.order_id, notif.id,
                )

        return {'processed': processed, 'errors': errors, 'total': len(notifications)}

    def action_auto_link_all_products_by_sku(self):
        """Auto-relaciona productos de Odoo con publicaciones de TiendaNube por SKU"""
        self.ensure_one()
        return self.env['tn.publication'].action_auto_link_all_products_by_sku()


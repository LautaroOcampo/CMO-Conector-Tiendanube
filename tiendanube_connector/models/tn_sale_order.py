# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError
import logging
import re
from datetime import datetime


_logger = logging.getLogger(__name__)


class TNSaleOrder(models.Model):
    """Ventas importadas desde TiendaNube"""
    _name = 'tn.sale.order'
    _description = 'Venta de TiendaNube'
    _rec_name = 'tn_order_id'
    _order = 'date_created desc'

    tn_order_id = fields.Integer(
        string='ID de Venta TiendaNube',
        required=True,
        copy=False,
        index=True,
        help='ID único de la venta en TiendaNube'
    )

    name = fields.Char(
        string='Número de Venta',
        compute='_compute_name',
        store=True,
        help='Número único de la venta generado por Odoo'
    )

    status = fields.Selection([
        ('open', 'Abierta'),
        ('closed', 'Cerrada'),
        ('payment_required', 'Pago Requerido'),
        ('cancelled', 'Cancelada'),
        ('abandoned', 'Abandonada'),
    ], string='Estado', required=True, default='open', index=True)

    date_created = fields.Datetime(
        string='Fecha de Creación',
        required=True,
        index=True
    )

    date_closed = fields.Datetime(
        string='Fecha de Cierre',
        help='Fecha en que se cerró la venta'
    )

    total = fields.Float(
        string='Total',
        required=True,
        digits=(16, 2),
        help='Monto total de la venta'
    )

    currency_id = fields.Many2one(
        'res.currency',
        string='Moneda',
        default=lambda self: self.env.company.currency_id
    )

    # Información del cliente
    customer_name = fields.Char(string='Nombre Completo')
    customer_email = fields.Char(string='Email')
    customer_phone = fields.Char(string='Teléfono')
    customer_dni = fields.Char(
        string='Identificación fiscal',
        help='Documento del cliente según TiendaNube (contact_identification); el formato depende del país (ej. DNI/CUIT en AR, CPF/CNPJ en BR).',
    )
    tn_customer_id = fields.Char(
        string='ID cliente TN',
        copy=False,
        index=True,
        help='customer.id de TiendaNube en el payload del pedido (único por cliente en TN).',
    )
    shipping_street = fields.Char(string='Calle')
    shipping_street_number = fields.Char(string='Número de Calle')
    shipping_city = fields.Char(string='Ciudad')
    shipping_state = fields.Char(string='Provincia')
    shipping_country = fields.Char(string='País')
    shipping_zip = fields.Char(string='Código Postal')
    
    # Información adicional
    origin = fields.Char(string='Origen', help='Origen de la venta (ej: web, app, etc.)')

    # Relaciones
    line_ids = fields.One2many(
        'tn.sale.order.line',
        'order_id',
        string='Líneas de Venta'
    )

    # Información adicional
    notes = fields.Text(string='Notas')
    payment_status = fields.Selection([
        ('pending', 'Pendiente'),
        ('paid', 'Pagada'),
        ('refunded', 'Reembolsada'),
    ], string='Estado de Pago', default='pending')

    # Estado de envío
    fulfillment_status = fields.Selection([
        ('to_pack', 'Por Empaquetar'),
        ('to_ship', 'Por Enviar'),
        ('shipped', 'Enviado'),
    ], string='Estado de Envío', default='to_pack', index=True)

    shipping_cost = fields.Float(
        string='Costo de Envío',
        digits=(16, 2),
        help='Valor del envío de la orden'
    )

    # Campos técnicos
    webhook_received_at = fields.Datetime(
        string='Webhook Recibido',
        help='Fecha y hora en que se recibió el webhook'
    )

    is_duplicate = fields.Boolean(
        string='Es Duplicado',
        default=False,
        help='Indica si esta venta ya fue procesada anteriormente'
    )

    partner_id = fields.Many2one(
        'res.partner',
        string='Contacto facturación (CF)',
        ondelete='set null',
        help='Consumidor Final usado en la orden de venta Odoo y la factura.',
    )
    buyer_partner_id = fields.Many2one(
        'res.partner',
        string='Contacto comprador',
        ondelete='set null',
        copy=False,
        help='Contacto opcional del comprador TN (solo si «Crear contacto del comprador» está activo en la config).',
    )
    
    odoo_sale_order_id = fields.Many2one(
        'sale.order',
        string='Orden de Venta Odoo',
        ondelete='set null',
        help='Orden de venta creada en Odoo desde esta venta de TiendaNube'
    )

    syncing_to_tn = fields.Boolean(
        string='Sincronizando a TiendaNube',
        default=False,
        help='Flag para evitar loops infinitos al sincronizar'
    )

    company_id = fields.Many2one(
        'res.company',
        string='Compañía',
        default=lambda self: self.env.company,
        required=True
    )
    
    tn_config_id = fields.Many2one(
        'tn.config',
        string='Configuración TiendaNube',
        help='Configuración de TiendaNube usada para importar esta orden',
        index=True
    )

    publication_count = fields.Integer(
        string='Cantidad de Publicaciones',
        compute='_compute_publication_count',
        help='Cantidad de publicaciones relacionadas con esta venta'
    )
    
    @api.depends('line_ids.publication_id')
    def _compute_publication_count(self):
        """Calcula la cantidad de publicaciones únicas relacionadas con la venta"""
        for record in self:
            publication_ids = record.line_ids.mapped('publication_id').filtered(lambda p: p)
            record.publication_count = len(publication_ids)
    
    @api.depends('tn_order_id', 'date_created')
    def _compute_name(self):
        """Genera un nombre único para la venta"""
        for record in self:
            if record.tn_order_id:
                record.name = f"TN-{record.tn_order_id}"
            else:
                record.name = _('Nueva Venta TN')

    _tn_order_id_company_uniq = models.Constraint(
        'UNIQUE(tn_order_id, company_id)',
        'Ya existe una venta con este ID de TiendaNube para esta compañía!',
    )

    @api.model
    def create_order_from_webhook(self, order_data, webhook_secret=None):
        """
        Crea o actualiza una venta desde datos del webhook de TiendaNube
        
        Args:
            order_data: Dict con datos de la venta desde TiendaNube
            webhook_secret: Secret del webhook para validación (opcional)
        
        Returns:
            tn.sale.order: Registro creado o actualizado
        """
        try:
            # Obtener la configuración de TiendaNube desde el contexto si está disponible
            tn_config_id = self.env.context.get('tn_config_id')
            tn_config = None
            if tn_config_id:
                tn_config = self.env['tn.config'].browse(tn_config_id)
                if tn_config.exists():
                    _logger.info("📋 Usando configuración específica: Store ID %s (ID: %s)", 
                               tn_config.store_id, tn_config_id)
                else:
                    _logger.warning("⚠️ Configuración ID %s no encontrada, usando configuración por defecto", tn_config_id)
                    tn_config = None
            
            # Si no hay configuración específica, usar la por defecto
            if not tn_config:
                tn_config = self.env['tn.config'].get_config()
                _logger.info("📋 Usando configuración por defecto: Store ID %s", tn_config.store_id if tn_config else 'N/A')
            
            # Log resumido en INFO; payload completo solo en DEBUG (evitar volumen y datos sensibles en producción)
            import json
            tn_order_id = order_data.get('id')
            _logger.info("📦 Orden TN recibida: id=%s", tn_order_id)
            if _logger.isEnabledFor(logging.DEBUG):
                order_json = json.dumps(order_data, indent=2, ensure_ascii=False, default=str)
                _logger.debug("📦 ORDEN COMPLETA (DEBUG): %s", order_json)
            if not tn_order_id:
                raise ValidationError(_('El webhook no contiene ID de venta'))

            # Buscar venta existente (idempotencia); usar compañía de la config si existe
            company_id = tn_config.company_id.id if tn_config and tn_config.company_id else self.env.company.id
            existing_order = self.search([
                ('tn_order_id', '=', tn_order_id),
                ('company_id', '=', company_id)
            ], limit=1)

            if existing_order:
                _logger.info("⚠️ Venta ya existe (idempotencia): TN Order ID %s (Odoo ID: %s)", 
                           tn_order_id, existing_order.id)
                existing_order.is_duplicate = True
                status_raw = str(order_data.get('status', 'open') or 'open').lower()
                status_map = {
                    'open': 'open',
                    'closed': 'closed',
                    'payment_required': 'payment_required',
                    'cancelled': 'cancelled',
                    'abandoned': 'abandoned',
                }
                mapped_status = status_map.get(status_raw, 'open')
                mapped_payment_status = existing_order._map_payment_status(order_data, mapped_status)
                
                # Actualizar estado de envío si cambió
                fulfillment_status = 'to_pack'
                # Intentar obtener fulfillment_status desde diferentes ubicaciones en el JSON
                fulfillment_status_raw = (
                    order_data.get('fulfillment_status', '') or 
                    order_data.get('fulfillment', {}).get('status', '') or
                    order_data.get('fulfillment_status', '')
                )
                
                # Log para debugging
                _logger.info("📦 Estado de envío recibido desde TiendaNube: '%s' (raw: %s)", 
                           fulfillment_status_raw, order_data.get('fulfillment_status') or order_data.get('fulfillment', {}))
                
                if fulfillment_status_raw:
                    fulfillment_map = {
                        'unfulfilled': 'to_pack',
                        'to_pack': 'to_pack',
                        'packed': 'to_ship',
                        'fulfilled': 'to_ship',
                        'to_ship': 'to_ship',
                        'ready_to_ship': 'to_ship',
                        'shipped': 'shipped',
                        'delivered': 'shipped',
                        'partial': 'to_ship',
                    }
                    fulfillment_status = fulfillment_map.get(str(fulfillment_status_raw).lower(), 'to_pack')
                    _logger.info("📦 Estado de envío mapeado: '%s' -> '%s'", fulfillment_status_raw, fulfillment_status)
                else:
                    _logger.warning("⚠️ No se encontró fulfillment_status en los datos de la orden")
                
                vals_to_update = {'syncing_to_tn': False}

                # Actualizar estado de envío solo como referencia (la gestión se hace en Odoo, no se disparan reserva ni validación desde TN)
                if existing_order.fulfillment_status != fulfillment_status:
                    _logger.info("🔄 Actualizando estado de envío (solo referencia) de '%s' a '%s' para orden TN %s", 
                               existing_order.fulfillment_status, fulfillment_status, existing_order.tn_order_id)
                    vals_to_update['fulfillment_status'] = fulfillment_status
                else:
                    _logger.info("ℹ️ Estado de envío sin cambios: '%s'", fulfillment_status)

                if existing_order.status != mapped_status:
                    _logger.info("🔄 Actualizando estado TN de '%s' a '%s' para orden %s",
                               existing_order.status, mapped_status, existing_order.tn_order_id)
                    vals_to_update['status'] = mapped_status

                if existing_order.payment_status != mapped_payment_status:
                    _logger.info("🔄 Actualizando estado de pago TN de '%s' a '%s' para orden %s",
                               existing_order.payment_status, mapped_payment_status, existing_order.tn_order_id)
                    vals_to_update['payment_status'] = mapped_payment_status

                # Contacto: CF en venta/factura; comprador opcional según config.
                if tn_config:
                    contact_vals = existing_order._tn_prepare_contact_vals(tn_config)
                    if contact_vals:
                        vals_to_update.update(contact_vals)

                cust_up = order_data.get('customer', {}) or {}
                tid_up = cust_up.get('id')
                if tid_up and not existing_order.tn_customer_id:
                    vals_to_update['tn_customer_id'] = str(tid_up).strip()

                if len(vals_to_update) > 1:
                    existing_order.write(vals_to_update)
                else:
                    existing_order.write({'syncing_to_tn': False})

                # Lógica solicitada:
                # - payment_required => crear/publicar factura sin registrar pago
                # - paid => registrar pago automáticamente si hay saldo pendiente
                existing_order._process_payment_state_from_tn(tn_config=tn_config)
                
                return existing_order

            # Parsear fecha
            date_created_str = order_data.get('created_at') or order_data.get('date_created')
            date_created = False
            if date_created_str:
                try:
                    # TiendaNube usa formato ISO 8601: "2023-12-18T10:30:00-03:00" o "2023-12-18T10:30:00"
                    if isinstance(date_created_str, str):
                        # Eliminar información de timezone si existe
                        if '+' in date_created_str or date_created_str.count('-') > 2:
                            date_created_str = date_created_str.split('+')[0].split('-03:00')[0].split('-04:00')[0]
                        # Reemplazar T por espacio
                        date_created_str = date_created_str.replace('T', ' ')
                        # Asegurar formato correcto
                        if ' ' in date_created_str:
                            date_part, time_part = date_created_str.split(' ', 1)
                            time_part = time_part.split('.')[0]  # Remover milisegundos si existen
                            date_created_str = f"{date_part} {time_part}"
                        date_created = fields.Datetime.from_string(date_created_str)
                except Exception as e:
                    _logger.warning("⚠️ Error parseando fecha '%s': %s", date_created_str, e)
                    date_created = fields.Datetime.now()

            if not date_created:
                date_created = fields.Datetime.now()

            # Parsear estado
            status = order_data.get('status', 'open')
            status_map = {
                'open': 'open',
                'closed': 'closed',
                'payment_required': 'payment_required',
                'cancelled': 'cancelled',
                'abandoned': 'abandoned',
            }
            status = status_map.get(status, 'open')

            # Parsear total
            total = float(order_data.get('total', 0) or 0)

            # Parsear costo de envío
            shipping_cost = float(order_data.get('shipping_cost', 0) or 0)
            if not shipping_cost:
                # Intentar obtener desde shipping_min_cost o shipping_cost_details
                shipping_cost = float(order_data.get('shipping_min_cost', 0) or 0)
                if not shipping_cost and order_data.get('shipping_cost_details'):
                    shipping_cost_details = order_data.get('shipping_cost_details', {})
                    if isinstance(shipping_cost_details, dict):
                        shipping_cost = float(shipping_cost_details.get('cost', 0) or 0)

            # Parsear estado de fulfillment (envío)
            fulfillment_status = 'to_pack'  # Por defecto: Por Empaquetar
            # Intentar obtener fulfillment_status desde diferentes ubicaciones en el JSON
            fulfillment_status_raw = (
                order_data.get('fulfillment_status', '') or 
                order_data.get('fulfillment', {}).get('status', '') or
                order_data.get('fulfillment_status', '')
            )
            
            # Log para debugging
            _logger.info("📦 Estado de envío recibido desde TiendaNube (nueva orden): '%s' (raw: %s)", 
                       fulfillment_status_raw, order_data.get('fulfillment_status') or order_data.get('fulfillment', {}))
            
            if fulfillment_status_raw:
                fulfillment_map = {
                    'unfulfilled': 'to_pack',  # Por Empaquetar
                    'to_pack': 'to_pack',  # Por Empaquetar
                    'packed': 'to_ship',  # Empaquetado = Por Enviar
                    'fulfilled': 'to_ship',  # Empaquetado = Por Enviar
                    'to_ship': 'to_ship',  # Por Enviar
                    'ready_to_ship': 'to_ship',  # Listo para enviar = Por Enviar
                    'shipped': 'shipped',  # Enviado
                    'delivered': 'shipped',  # Entregado también se considera enviado
                    'partial': 'to_ship',  # Parcialmente enviado = Por Enviar
                }
                fulfillment_status = fulfillment_map.get(str(fulfillment_status_raw).lower(), 'to_pack')
                _logger.info("📦 Estado de envío mapeado (nueva orden): '%s' -> '%s'", fulfillment_status_raw, fulfillment_status)
            else:
                _logger.warning("⚠️ No se encontró fulfillment_status en los datos de la orden nueva")

            # Información del cliente según documentación oficial de TiendaNube
            # https://tiendanube.github.io/api-documentation/resources/order#get-ordersid
            # Basado en el JSON de ejemplo proporcionado
            
            # Nombre completo: desde contact_name (campo directo) o customer.name o shipping_address.name
            customer = order_data.get('customer', {}) or {}
            tn_customer_raw = customer.get('id')
            tn_customer_id = str(tn_customer_raw).strip() if tn_customer_raw not in (None, False, '') else ''

            customer_name = (order_data.get('contact_name', '') or 
                           customer.get('name', '') or 
                           order_data.get('shipping_address', {}).get('name', ''))
            
            # Email: desde contact_email (campo directo del order según documentación)
            customer_email = order_data.get('contact_email', '') or customer.get('email', '')
            
            # Teléfono: desde contact_phone (campo directo del order) o shipping_address.phone
            customer_phone = (order_data.get('contact_phone', '') or 
                            order_data.get('shipping_address', {}).get('phone', '') or
                            customer.get('phone', ''))
            
            # DNI/CUIT: desde contact_identification (campo directo del order según documentación)
            # Puede venir como string directamente: "75839566500"
            customer_dni = ''
            contact_identification = order_data.get('contact_identification', '')
            if contact_identification:
                if isinstance(contact_identification, dict):
                    customer_dni = contact_identification.get('number', '') or contact_identification.get('value', '')
                else:
                    customer_dni = str(contact_identification).strip()
            
            _logger.info("👤 Cliente extraído: nombre='%s', email='%s', tel='%s', DNI='%s'", 
                        customer_name, customer_email, customer_phone, customer_dni)
            
            # Dirección de envío según documentación oficial
            # https://tiendanube.github.io/api-documentation/resources/order#get-ordersid
            # shipping_address contiene: address, city, country, name, number, phone, province, zipcode
            shipping_address = order_data.get('shipping_address', {}) or {}
            
            if isinstance(shipping_address, dict):
                # Según documentación: address, number, city, province, country, zipcode
                shipping_street = shipping_address.get('address', '')
                shipping_street_number = shipping_address.get('number', '')
                shipping_city = shipping_address.get('city', '')
                shipping_state = shipping_address.get('province', '')
                shipping_country = shipping_address.get('country', '')
                shipping_zip = shipping_address.get('zipcode', '')
            else:
                shipping_street = shipping_street_number = shipping_city = shipping_state = shipping_country = shipping_zip = ''
            
            _logger.info(
                "📦 Dirección extraída (TN): calle='%s', número='%s', ciudad='%s', provincia='%s', país TN='%s', CP='%s'",
                shipping_street, shipping_street_number, shipping_city, shipping_state, shipping_country, shipping_zip,
            )
            shipping_country = self._tn_shipping_country_display_name(
                shipping_country, tn_config
            )

            # Origen de la venta: desde source (campo directo del order según documentación)
            origin = order_data.get('source', '')
            
            # Notas: desde note (nota del cliente) y owner_note (nota del dueño)
            note = order_data.get('note', '') or ''
            owner_note = order_data.get('owner_note', '') or ''
            notes = ''
            if note and owner_note:
                notes = f"Cliente: {note}\n\nDueño: {owner_note}"
            elif note:
                notes = f"Cliente: {note}"
            elif owner_note:
                notes = f"Dueño: {owner_note}"

            # Crear venta
            vals = {
                'tn_order_id': tn_order_id,
                'status': status,
                'date_created': date_created,
                'total': total,
                'customer_name': customer_name,
                'customer_email': customer_email,
                'customer_phone': customer_phone,
                'customer_dni': customer_dni,
                'tn_customer_id': tn_customer_id or False,
                'shipping_street': shipping_street,
                'shipping_street_number': shipping_street_number,
                'shipping_city': shipping_city,
                'shipping_state': shipping_state,
                'shipping_country': shipping_country,
                'shipping_zip': shipping_zip,
                'origin': origin,
                'notes': notes,
                'payment_status': self._map_payment_status(order_data, status),
                'fulfillment_status': fulfillment_status,
                'shipping_cost': shipping_cost,
                'webhook_received_at': fields.Datetime.now(),
                'company_id': tn_config.company_id.id if tn_config and tn_config.company_id else self.env.company.id,
                'tn_config_id': tn_config.id if tn_config else False,
            }

            if status == 'closed' and order_data.get('closed_at'):
                try:
                    date_closed_str = order_data.get('closed_at')
                    if isinstance(date_closed_str, str):
                        if '+' in date_closed_str or date_closed_str.count('-') > 2:
                            date_closed_str = date_closed_str.split('+')[0].split('-03:00')[0].split('-04:00')[0]
                        date_closed_str = date_closed_str.replace('T', ' ')
                        if ' ' in date_closed_str:
                            date_part, time_part = date_closed_str.split(' ', 1)
                            time_part = time_part.split('.')[0]
                            date_closed_str = f"{date_part} {time_part}"
                        vals['date_closed'] = fields.Datetime.from_string(date_closed_str)
                except Exception as e:
                    _logger.warning("⚠️ Error parseando fecha de cierre: %s", e)

            # Asegurar que syncing_to_tn esté en False al crear (viene del webhook)
            vals['syncing_to_tn'] = False

            order = self.create(vals)
            if tn_config:
                order._tn_sync_contacts_from_config(tn_config)

            _logger.info("✅ Venta creada desde webhook: TN Order ID %s (Odoo ID: %s)",
                        tn_order_id, order.id)

            # Crear líneas de venta
            line_items = order_data.get('products', []) or order_data.get('line_items', []) or []
            for line_item in line_items:
                self.env['tn.sale.order.line'].create_line_from_webhook(order, line_item)

            # Crear orden de venta en Odoo solo si está habilitado en la configuración
            auto_create_so = bool(tn_config and tn_config.auto_create_sale_order)
            if auto_create_so:
                try:
                    _logger.info("🔄 Iniciando creación de orden de venta Odoo para TN Order ID %s", tn_order_id)
                    # Marcar que viene del webhook para evitar sincronización de vuelta
                    order.write({'syncing_to_tn': False})
                    order.create_odoo_sale_order()

                    # Asegurar que el partner_id esté guardado si se creó en create_odoo_sale_order
                    if order.odoo_sale_order_id and order.odoo_sale_order_id.partner_id:
                        if not order.partner_id or order.partner_id.id != order.odoo_sale_order_id.partner_id.id:
                            order.partner_id = order.odoo_sale_order_id.partner_id.id
                            _logger.info("✅ Partner ID guardado desde orden de venta Odoo: %s", order.partner_id.name)

                    # Verificar que se creó correctamente
                    if order.odoo_sale_order_id:
                        _logger.info("✅ Orden de venta Odoo creada exitosamente: %s", order.odoo_sale_order_id.name)
                    else:
                        _logger.error("❌ No se pudo crear la orden de venta Odoo (odoo_sale_order_id es None)")
                except Exception as e:
                    _logger.exception("❌ Error creando orden de venta en Odoo: %s", e)
                    # No fallar el webhook si falla la creación de la orden de venta
            else:
                _logger.info(
                    "ℹ️ auto_create_sale_order deshabilitado: venta TN %s guardada sin orden Odoo (usar botón manual).",
                    tn_order_id,
                )

            # Aplicar reglas de facturación/pago según estado TN (solo si hay SO).
            order._process_payment_state_from_tn(tn_config=tn_config)

            if order.status == 'cancelled' and order.odoo_sale_order_id:
                order._sync_odoo_on_tn_cancelled()

            # El estado de envío de TN no dispara reserva ni validación en Odoo; la gestión se hace en Odoo

            # Actualizar stock en publicaciones TN desde Odoo y verificar pausa/activación por stock mínimo.
            # Tras vender en TN el stock en Odoo ya se descontó; las publicaciones deben refrescarse
            # desde Odoo para que current_stock_tn y la lógica de pausa usen el valor correcto.
            try:
                order._refresh_tn_publications_stock_and_check_min_stock()
            except Exception as e:
                _logger.exception("❌ Error refrescando publicaciones TN y verificando stock mínimo: %s", e)
                # No fallar el webhook por esto

            return order

        except Exception as e:
            _logger.exception("❌ Error creando venta desde webhook: %s", e)
            raise

    def action_view_lines(self):
        """Acción para ver las líneas de la venta"""
        self.ensure_one()
        return {
            'name': _('Líneas de Venta'),
            'type': 'ir.actions.act_window',
            'res_model': 'tn.sale.order.line',
            'view_mode': 'list,form',
            'domain': [('order_id', '=', self.id)],
            'context': {'default_order_id': self.id},
            'target': 'current',
        }

    def action_view_contact(self):
        """Abre el contacto comprador (si existe) o el Consumidor Final de la venta."""
        self.ensure_one()
        partner = self.buyer_partner_id or self.partner_id
        if not partner:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Sin contacto'),
                    'message': _('No hay contacto relacionado con esta venta de TiendaNube.'),
                    'type': 'warning',
                    'sticky': False,
                }
            }
        
        return {
            'name': _('Contacto'),
            'type': 'ir.actions.act_window',
            'res_model': 'res.partner',
            'res_id': partner.id,
            'view_mode': 'form',
            'target': 'current',
        }
    
    def action_view_publication(self):
        """Abre el formulario de la publicación relacionada con la venta (la primera si hay varias)."""
        self.ensure_one()
        publications = self.line_ids.mapped('publication_id').filtered(lambda p: p)
        if not publications:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Sin publicaciones'),
                    'message': _('No hay publicaciones relacionadas con esta venta de TiendaNube.'),
                    'type': 'warning',
                    'sticky': False,
                }
            }
        publication = publications[0]
        return {
            'name': _('Publicación'),
            'type': 'ir.actions.act_window',
            'res_model': 'tn.publication',
            'res_id': publication.id,
            'view_mode': 'form',
            'target': 'current',
        }
    
    def action_view_odoo_sale_order(self):
        """Acción para ver la orden de venta de Odoo relacionada"""
        self.ensure_one()
        if not self.odoo_sale_order_id:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Sin orden de venta'),
                    'message': _('No hay orden de venta de Odoo relacionada con esta venta de TiendaNube.'),
                    'type': 'warning',
                    'sticky': False,
                }
            }
        
        return {
            'name': _('Orden de Venta'),
            'type': 'ir.actions.act_window',
            'res_model': 'sale.order',
            'res_id': self.odoo_sale_order_id.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def _refresh_tn_publications_stock_and_check_min_stock(self):
        """
        Tras crear la orden de venta en Odoo, el stock se descontará al validar los albaranes.
        Actualiza el stock en las publicaciones TN desde Odoo y ejecuta la verificación de
        pausa/activación por stock mínimo.
        """
        self.ensure_one()
        publications = self.line_ids.mapped('publication_id').filtered(
            lambda p: p and p.tn_product_id and p.odoo_product_id
        )
        if not publications:
            return
        _logger.info(
            "🔄 Refrescando stock en %d publicación(es) TN desde Odoo y verificando pausa/activación (TN Order %s)",
            len(publications), self.tn_order_id
        )
        for pub in publications:
            try:
                pub._sync_stock_and_price_from_odoo()
                pub._check_and_update_published_status()
            except Exception as e:
                _logger.exception("❌ Error refrescando publicación TN '%s': %s", pub.title, e)

    def _tn_resolve_product_for_sale_line(self, line, fallback_product):
        """Resuelve product.product para una tn.sale.order.line; indica si se usó fallback."""
        product = None
        product_variant = None

        if line.publication_id and line.publication_id.odoo_product_id:
            product = line.publication_id.odoo_product_id
            if line.publication_variant_id and line.publication_variant_id.odoo_variant_id:
                product = line.publication_variant_id.odoo_variant_id.product_tmpl_id
                product_variant = line.publication_variant_id.odoo_variant_id
            else:
                product_variant = product.product_variant_id
        elif line.sku:
            product_variant = self.env['product.product'].search([
                ('default_code', '=', line.sku),
                ('company_id', '=', self.env.company.id),
            ], limit=1)
            if product_variant:
                product = product_variant.product_tmpl_id
            else:
                product = self.env['product.template'].search([
                    ('default_code', '=', line.sku),
                    ('company_id', '=', self.env.company.id),
                ], limit=1)
                if product:
                    product_variant = product.product_variant_id
                elif fallback_product:
                    return fallback_product, True

        if not product_variant and fallback_product and not product:
            return fallback_product, True

        if product and not product_variant:
            product_variant = product.product_variant_id

        if not product_variant:
            if fallback_product:
                return fallback_product, True
            return self.env['product.product'], False

        return product_variant, False

    def _tn_prepare_sale_order_line_vals(self, tn_line, product_variant, is_fallback):
        """Descripción interna con referencia TN; nombre limpio en PDF solo si hay fallback."""
        tn_title = (tn_line.name or '').strip()
        if is_fallback:
            line_name = '%s [TN %s]' % (tn_title or product_variant.display_name, self.tn_order_id)
        else:
            line_name = tn_line.name

        order_line_vals = {
            'product_id': product_variant.id,
            'product_uom_qty': tn_line.quantity,
            'price_unit': tn_line.price_unit,
            'name': line_name,
        }
        if is_fallback:
            display = tn_title or (tn_line.sku or '').strip() or product_variant.display_name
            if display:
                order_line_vals['tn_invoice_display_name'] = display
        return order_line_vals

    def _tn_hold_sale_order_for_fallback(self, used_fallback, config=None):
        if not used_fallback:
            return False
        config = config or self.tn_config_id or self.env['tn.config'].get_config()
        return not (config and config.auto_invoice_with_fallback_product)

    def create_odoo_sale_order(self):
        """
        Crea una orden de venta en Odoo desde esta venta de TiendaNube.
        Incluye creación del cliente, líneas de venta, confirmación y facturación.
        """
        self.ensure_one()

        # Verificar si ya existe una orden de venta
        if self.odoo_sale_order_id:
            _logger.info("⚠️ Ya existe una orden de venta Odoo para TN Order ID %s: %s", 
                        self.tn_order_id, self.odoo_sale_order_id.name)
            return self.odoo_sale_order_id

        # Obtener configuración (usar la configuración específica si está disponible)
        config = self.tn_config_id if self.tn_config_id else self.env['tn.config'].get_config()
        if not config.sale_pricelist_id:
            _logger.warning("⚠️ No hay lista de precios configurada para ventas. Usando lista por defecto.")
            pricelist = self.env['product.pricelist'].search([('company_id', '=', self.env.company.id)], limit=1)
            if not pricelist:
                pricelist = self.env.company.partner_id.property_product_pricelist
        else:
            pricelist = config.sale_pricelist_id

        # Consumidor Final para orden de venta (factura también va a CF).
        partner = self._get_consumidor_final_partner(config)
        if config.create_order_contacts:
            buyer = self._tn_create_or_update_buyer_contact()
            if buyer:
                self.buyer_partner_id = buyer.id
        
        # Guardar el partner_id en la venta de TiendaNube (siempre actualizar)
        self.partner_id = partner.id
        _logger.info("✅ Partner ID guardado en venta TN: %s (ID: %s)", partner.name, partner.id)

        # Crear líneas de venta
        order_lines = []
        fallback_product = config.fallback_product_id
        used_fallback_product = False
        for line in self.line_ids:
            product_variant, is_fallback = self._tn_resolve_product_for_sale_line(line, fallback_product)
            if not product_variant:
                if line.sku:
                    _logger.warning("⚠️ No se encontró producto con SKU %s", line.sku)
                else:
                    _logger.warning("⚠️ Línea sin publicación ni SKU: %s", line.name)
                continue

            if is_fallback:
                used_fallback_product = True
                _logger.warning(
                    "⚠️ Línea '%s' (SKU: %s). Usando fallback: %s (ID: %s)",
                    line.name, line.sku, product_variant.display_name, product_variant.id,
                )

            order_line_vals = self._tn_prepare_sale_order_line_vals(
                line, product_variant, is_fallback,
            )
            if config.default_tax_id:
                order_line_vals['tax_id'] = [(6, 0, [config.default_tax_id.id])]
            order_lines.append((0, 0, order_line_vals))

        if not order_lines:
            raise UserError(_('No se pudieron crear líneas de venta. Verifique que los productos estén vinculados correctamente.'))

        # Obtener almacén configurado
        warehouse = config.warehouse_id
        if not warehouse:
            _logger.warning("⚠️ No hay almacén configurado. Usando almacén por defecto de la compañía.")
            warehouse = self.env['stock.warehouse'].search([
                ('company_id', '=', self.env.company.id)
            ], limit=1)
        
        # Crear orden de venta
        tn_store_id = config.store_id or ''
        tn_api_base = (config.api_base_url or 'https://api.tiendanube.com/v1').rstrip('/')
        tn_order_api_url = f"{tn_api_base}/{tn_store_id}/orders/{self.tn_order_id}" if tn_store_id else ''
        tn_order_admin_url = f"https://www.tiendanube.com/admin/orders/{self.tn_order_id}"
        note_parts = [f"Venta TiendaNube #{self.tn_order_id}"]
        if tn_order_admin_url:
            note_parts.append(f"Link admin TN: {tn_order_admin_url}")
        if tn_order_api_url:
            note_parts.append(f"Link API TN: {tn_order_api_url}")
        if used_fallback_product:
            if config.auto_invoice_with_fallback_product:
                note_parts.append(
                    'INFO: Al menos una línea usa producto fallback; '
                    'se confirma y factura con el nombre TN en el PDF.'
                )
            else:
                note_parts.append(
                    'ATENCION: Se uso producto fallback/no identificado en al menos una linea. '
                    'Revisar antes de confirmar.'
                )

        sale_order_vals = {
            'partner_id': partner.id,
            'date_order': self.date_created,
            'pricelist_id': pricelist.id,
            'order_line': order_lines,
            'company_id': self.env.company.id,
            'note': '\n'.join(note_parts),
        }
        shipping_partner = self._tn_get_shipping_partner(partner)
        if shipping_partner and shipping_partner.id != partner.id:
            sale_order_vals['partner_shipping_id'] = shipping_partner.id
        
        # Agregar almacén si está configurado
        if warehouse:
            sale_order_vals['warehouse_id'] = warehouse.id
            _logger.info("📦 Usando almacén configurado: %s", warehouse.name)

        sale_order = self.env['sale.order'].create(sale_order_vals)
        _logger.info("✅ Orden de venta Odoo creada: %s para TN Order ID %s", sale_order.name, self.tn_order_id)

        # Vincular orden de venta (bidireccional)
        # Usar write con contexto para evitar procesamiento adicional
        # Establecer syncing_to_tn primero para evitar sincronización
        self.syncing_to_tn = False
        # Luego vincular la orden (esto llamará a write pero no procesará sincronización)
        self.odoo_sale_order_id = sale_order.id
        sale_order.tn_sale_order_id = self.id
        
        # Asegurar que el partner_id esté guardado en la venta de TiendaNube
        if sale_order.partner_id and (not self.partner_id or self.partner_id.id != sale_order.partner_id.id):
            self.partner_id = sale_order.partner_id.id
            _logger.info("✅ Partner ID vinculado a venta TN: %s (ID: %s)", self.partner_id.name, self.partner_id.id)

        # Confirmar la orden (borrador solo si fallback y la opción está desactivada).
        if self._tn_hold_sale_order_for_fallback(used_fallback_product, config):
            _logger.warning(
                "⚠️ Orden %s creada en borrador porque se usó producto fallback/no identificado. Requiere revisión manual.",
                sale_order.name
            )
        else:
            if used_fallback_product:
                _logger.info(
                    "ℹ️ Orden %s: líneas con producto fallback — confirmación y factura automática habilitadas.",
                    sale_order.name,
                )
            try:
                sale_order.action_confirm()
                _logger.info("✅ Orden de venta confirmada: %s", sale_order.name)
            except Exception as e:
                _logger.exception("❌ Error confirmando orden de venta: %s", e)
                raise

        # La orden de entrega queda activa: TN descuenta su stock y Odoo el suyo (sin duplicar);
        # no se cancela el albarán para que aparezca y se pueda reservar/validar en Odoo.

        # Crear factura solo si está habilitada la facturación automática
        if config.auto_invoice and sale_order.state in ('sale', 'done'):
            try:
                self._create_invoice_from_sale_order(sale_order, config)
            except Exception as e:
                _logger.exception("❌ Error creando factura: %s", e)
                # No fallar si no se puede crear la factura
        elif config.auto_invoice:
            _logger.info("ℹ️ Facturación automática omitida para %s: la orden está en borrador por revisión de fallback.", sale_order.name)
        else:
            _logger.info("ℹ️ Facturación automática deshabilitada. La factura no se creará automáticamente.")

        return sale_order

    @api.model
    def _tn_shipping_country_display_name(self, shipping_country_raw, tn_config):
        """Nombre del país para el campo informativo shipping_country (ISO, nombre o tienda API)."""
        raw = (shipping_country_raw or '').strip()
        Country = self.env['res.country']
        rec = Country.browse()
        if len(raw) == 2 and raw.isalpha():
            rec = Country.search([('code', '=', raw.upper())], limit=1)
        if not rec and raw:
            rec = Country.search([('name', 'ilike', raw)], limit=1)
        if not rec and tn_config:
            rec = tn_config.get_res_country_for_tn_store()
        if not rec and tn_config:
            rec = tn_config.get_fallback_country_for_partners()
        if not rec:
            rec = Country.search([('code', '=', 'AR')], limit=1)
        return rec.name if rec else (raw or '')

    def _tn_has_delivery_province(self):
        """True si el pedido trae provincia de entrega (TiendaNube shipping_address.province)."""
        self.ensure_one()
        return bool((self.shipping_state or '').strip())

    def _tn_res_country_for_new_partner(self):
        """País Odoo al crear contactos: país de la tienda (API) o Argentina si aún no hay datos."""
        self.ensure_one()
        config = self.tn_config_id or self.env['tn.config'].get_config()
        if config:
            c = config.get_fallback_country_for_partners()
            if c:
                return c
        country = self.env['res.country'].search([('code', '=', 'AR')], limit=1)
        if not country:
            _logger.warning(
                "⚠️ No se encontró res.country con código AR; el país del contacto puede quedar vacío."
            )
        return country

    def _get_consumidor_final_partner(self, config=None):
        """Consumidor Final para orden de venta y factura TN."""
        self.ensure_one()
        config = config or self.tn_config_id or self.env['tn.config'].get_config()
        partner = config.consumidor_final_partner_id if config else False
        if not partner:
            raise UserError(_(
                'Configure el contacto «Consumidor Final» en la configuración de TiendaNube.'
            ))
        return partner

    def _tn_prepare_contact_vals(self, config=None):
        """Valores de partner_id / buyer_partner_id sin escribir (webhook update)."""
        self.ensure_one()
        config = config or self.tn_config_id or self.env['tn.config'].get_config()
        if not config:
            return {}
        vals = {}
        try:
            cf = self._get_consumidor_final_partner(config)
            if self.partner_id != cf:
                vals['partner_id'] = cf.id
        except UserError:
            return vals
        if config.create_order_contacts:
            buyer = self._tn_create_or_update_buyer_contact()
            if buyer and self.buyer_partner_id != buyer:
                vals['buyer_partner_id'] = buyer.id
        elif self.buyer_partner_id:
            vals['buyer_partner_id'] = False
        return vals

    def _tn_sync_contacts_from_config(self, config=None):
        """CF en partner_id; comprador opcional en buyer_partner_id."""
        self.ensure_one()
        config = config or self.tn_config_id or self.env['tn.config'].get_config()
        if not config:
            return
        vals = self._tn_prepare_contact_vals(config)
        if vals:
            self.write(vals)
        cf = self.partner_id
        if cf:
            _logger.info(
                'TN Order %s: facturación = Consumidor Final (%s)',
                self.tn_order_id,
                cf.name,
            )
        if self.buyer_partner_id:
            _logger.info(
                'TN Order %s: comprador vinculado (%s)',
                self.tn_order_id,
                self.buyer_partner_id.name,
            )

    def _tn_create_or_update_buyer_contact(self):
        """
        Busca o crea el contacto del comprador TN (no se usa para facturar).
        """
        self.ensure_one()

        def _normalize(value):
            return re.sub(r'\s+', ' ', (value or '').strip())

        Partner = self.env['res.partner']
        normalized_name = _normalize(self.customer_name) or _('Cliente TiendaNube')
        normalized_email = _normalize(self.customer_email).lower()
        tn_cust_id = (self.tn_customer_id or '').strip()

        partner = Partner.browse()
        company_domain = [
            '|',
            ('company_id', '=', False),
            ('company_id', '=', self.env.company.id),
        ]

        if tn_cust_id:
            partner = Partner.search(
                [('tn_customer_id', '=', tn_cust_id)] + company_domain,
                limit=1,
            )
        if not partner and normalized_email:
            partner = Partner.search(
                [('email', '=ilike', normalized_email)] + company_domain,
                limit=1,
            )
        if not partner and self.customer_dni:
            dni_norm = re.sub(r'[^0-9]', '', str(self.customer_dni))
            if dni_norm:
                candidates = Partner.search(
                    company_domain + [
                        '|',
                        ('vat', 'ilike', dni_norm),
                        ('vat', 'ilike', self.customer_dni),
                    ],
                    limit=20,
                )
                for cand in candidates:
                    cand_vat = re.sub(r'[^0-9]', '', cand.vat or '')
                    if cand_vat == dni_norm:
                        partner = cand
                        break

        country = self._tn_res_country_for_new_partner()
        if self.shipping_country:
            by_name = self.env['res.country'].search(
                [('name', 'ilike', self.shipping_country)], limit=1,
            )
            if by_name:
                country = by_name

        normalized_state = _normalize(self.shipping_state)
        state = self.env['res.country.state'].browse()
        if normalized_state:
            state_domain = [('name', 'ilike', normalized_state)]
            if country:
                state = self.env['res.country.state'].search(
                    state_domain + [('country_id', '=', country.id)], limit=1,
                )
            if not state:
                state = self.env['res.country.state'].search(state_domain, limit=1)

        street = _normalize(self.shipping_street)
        street2 = (
            f"N° {self.shipping_street_number}".strip()
            if self.shipping_street_number
            else False
        )
        comment_parts = [
            'Cliente TiendaNube',
            f'Pedido TN #{self.tn_order_id}',
        ]
        if self.origin:
            comment_parts.append(f'Origen: {self.origin}')
        if self.notes:
            comment_parts.append(self.notes)
        comment = '\n'.join(comment_parts)

        if partner:
            update_vals = {}
            if tn_cust_id and not partner.tn_customer_id:
                update_vals['tn_customer_id'] = tn_cust_id
            if normalized_email and (not partner.email or partner.email.lower() != normalized_email):
                update_vals['email'] = normalized_email
            if self.customer_phone and not partner.phone:
                update_vals['phone'] = self.customer_phone
            if self.customer_dni and not partner.vat:
                update_vals['vat'] = self.customer_dni
            if normalized_name and partner.name in (False, '', 'Cliente TiendaNube'):
                update_vals['name'] = normalized_name
            if street and not partner.street:
                update_vals['street'] = street
            if street2 and not partner.street2:
                update_vals['street2'] = street2
            if self.shipping_city and not partner.city:
                update_vals['city'] = self.shipping_city
            if self.shipping_zip and not partner.zip:
                update_vals['zip'] = self.shipping_zip
            if state and not partner.state_id:
                update_vals['state_id'] = state.id
            if country and not partner.country_id:
                update_vals['country_id'] = country.id
            if update_vals:
                partner.write(update_vals)
            _logger.info(
                'TN Order %s: comprador reutilizado %s (ID %s)',
                self.tn_order_id, partner.name, partner.id,
            )
            return partner

        partner_vals = {
            'name': normalized_name,
            'email': normalized_email or False,
            'phone': self.customer_phone or False,
            'vat': self.customer_dni or False,
            'street': street or False,
            'street2': street2 or False,
            'city': self.shipping_city or False,
            'zip': self.shipping_zip or False,
            'comment': comment,
            'is_company': False,
        }
        if tn_cust_id:
            partner_vals['tn_customer_id'] = tn_cust_id
        if country:
            partner_vals['country_id'] = country.id
        if state:
            partner_vals['state_id'] = state.id
        elif normalized_state:
            partner_vals['comment'] = f'{comment}\nProvincia: {normalized_state}'

        if self.customer_dni and 'l10n_latam_identification_type_id' in Partner._fields:
            dni_clean = re.sub(r'[^0-9]', '', str(self.customer_dni))
            IdType = self.env['l10n_latam.identification.type']
            if len(dni_clean) == 11:
                id_type = IdType.search([('name', 'ilike', 'CUIT')], limit=1)
            else:
                id_type = IdType.search([('name', 'ilike', 'DNI')], limit=1)
            if id_type:
                partner_vals['l10n_latam_identification_type_id'] = id_type.id

        lang = Partner._connector_argentina_lang()
        if lang:
            partner_vals['lang'] = lang

        partner = Partner.create(partner_vals)
        _logger.info(
            'TN Order %s: comprador creado %s (ID %s)',
            self.tn_order_id, partner.name, partner.id,
        )
        return partner

    def _tn_get_shipping_partner(self, partner):
        """
        Dirección de entrega: hijo delivery del partner comercial (CF) con datos del pedido TN.
        """
        self.ensure_one()
        if not partner:
            return self.env['res.partner']
        if not (self.shipping_street or self.shipping_city):
            return partner

        street = (self.shipping_street or '').strip()
        street2 = (
            f"N° {self.shipping_street_number}".strip()
            if self.shipping_street_number
            else ''
        )
        city = (self.shipping_city or '').strip()
        zipcode = (self.shipping_zip or '').strip()
        ship_name = (self.customer_name or '').strip() or partner.name

        Partner = self.env['res.partner']
        existing = Partner.search([
            ('parent_id', '=', partner.id),
            ('type', '=', 'delivery'),
            ('street', '=', street),
            ('zip', '=', zipcode),
        ], limit=1)
        if existing:
            return existing

        state = partner.state_id
        if not state and self.shipping_state:
            normalized_state = re.sub(r'\s+', ' ', self.shipping_state.strip())
            country = partner.country_id or self._tn_res_country_for_new_partner()
            if country:
                state = self.env['res.country.state'].search([
                    ('name', 'ilike', normalized_state),
                    ('country_id', '=', country.id),
                ], limit=1)

        delivery_vals = {
            'parent_id': partner.id,
            'type': 'delivery',
            'name': ship_name,
            'street': street,
            'street2': street2 or False,
            'city': city or False,
            'zip': zipcode or False,
            'state_id': state.id if state else False,
            'country_id': partner.country_id.id if partner.country_id else False,
            'phone': self.customer_phone or partner.phone or False,
        }
        lang = Partner._connector_argentina_lang()
        if lang:
            delivery_vals['lang'] = lang
        return Partner.create(delivery_vals)

    def _validate_delivery(self):
        """
        Valida el picking de entrega cuando el estado de envío es "shipped".
        Cuando se valida el envío, se marca como 'fulfilled' (empaquetado) en TiendaNube.
        """
        self.ensure_one()
        
        _logger.info("=" * 80)
        _logger.info("🚀 INICIO: Validación de envío desde _validate_delivery")
        _logger.info("📦 Orden TiendaNube: TN Order ID %s (Odoo ID: %s)", self.tn_order_id, self.id)
        _logger.info("📊 Estado actual de envío: %s", self.fulfillment_status)
        _logger.info("🛒 Orden de venta Odoo: %s (ID: %s)", 
                    self.odoo_sale_order_id.name if self.odoo_sale_order_id else 'N/A',
                    self.odoo_sale_order_id.id if self.odoo_sale_order_id else 'N/A')

        if not self.odoo_sale_order_id:
            _logger.warning("⚠️ No hay orden de venta Odoo para validar envío: TN Order ID %s", self.tn_order_id)
            _logger.info("=" * 80)
            return False

        # Buscar pickings de entrega relacionados con la orden de venta
        _logger.info("🔍 Buscando pickings de salida para orden Odoo %s", self.odoo_sale_order_id.id)
        pickings = self.env['stock.picking'].search([
            ('sale_id', '=', self.odoo_sale_order_id.id),
            ('picking_type_id.code', '=', 'outgoing'),  # Solo pickings de salida
            ('state', 'in', ['assigned', 'waiting', 'confirmed']),  # Estados que se pueden validar
        ])

        if not pickings:
            _logger.warning("⚠️ No se encontraron pickings para validar: Orden Odoo %s", self.odoo_sale_order_id.name)
            _logger.info("=" * 80)
            return False

        _logger.info("📦 Pickings encontrados: %d", len(pickings))
        for picking in pickings:
            _logger.info("  - Picking: %s (ID: %s, Estado: %s)", picking.name, picking.id, picking.state)

        validated_count = 0
        for picking in pickings:
            try:
                # Validar el picking usando button_validate
                if picking.state in ['assigned', 'waiting', 'confirmed']:
                    _logger.info("✅ Validando picking: %s (Estado actual: %s)", picking.name, picking.state)
                    picking.button_validate()
                    validated_count += 1
                    _logger.info("✅ Picking validado exitosamente: %s (Nuevo estado: %s)", 
                               picking.name, picking.state)
                else:
                    _logger.warning("⚠️ Picking %s no está en estado válido para validar: %s", 
                                  picking.name, picking.state)
            except Exception as e:
                _logger.exception("❌ Error validando picking %s: %s", picking.name, e)
                continue

        if validated_count > 0:
            _logger.info("✅ %d picking(s) validado(s) para orden %s", validated_count, self.odoo_sale_order_id.name)
            
            # Cuando se valida el envío en Odoo, se marca el fulfillment order como 'delivered' (entregado) en TiendaNube
            _logger.info("🔄 Validación de envío completada. Marcando fulfillment order como 'delivered' en TiendaNube")
            
            try:
                # Marcar fulfillment order como 'delivered' usando la API de Fulfillment Orders
                delivered_result = self._mark_fulfillment_order_as_delivered()
                
                if delivered_result:
                    _logger.info("✅ Fulfillment order marcado como 'delivered' exitosamente en TiendaNube")
                else:
                    _logger.warning("⚠️ No se pudo marcar fulfillment order como 'delivered' en TiendaNube")
                
                # Actualizar el estado local a 'shipped' para reflejar que está entregado
                if self.fulfillment_status != 'shipped':
                    _logger.info("🔄 Actualizando estado local de '%s' a 'shipped' (entregado)", self.fulfillment_status)
                    self.write({
                        'fulfillment_status': 'shipped',
                        'syncing_to_tn': False  # Viene de validación manual, no del webhook
                    })
                    _logger.info("✅ Estado local actualizado a 'shipped'")
                else:
                    _logger.info("ℹ️ Estado ya está en 'shipped', no se actualiza")
                    
            except Exception as e:
                _logger.exception("❌ Error marcando fulfillment order como 'delivered' después de validar: %s", e)
                # Aún así, actualizar el estado local
                try:
                    if self.fulfillment_status != 'shipped':
                        self.write({
                            'fulfillment_status': 'shipped',
                            'syncing_to_tn': False
                        })
                except Exception as e2:
                    _logger.exception("❌ Error actualizando estado local: %s", e2)
            
            return True
        else:
            _logger.warning("⚠️ No se pudo validar ningún picking para orden %s", self.odoo_sale_order_id.name)
            return False

    def _reserve_stock(self):
        """
        Reserva el stock en los pickings de la orden de venta cuando el estado es 'to_pack' o 'to_ship'.
        """
        self.ensure_one()

        if not self.odoo_sale_order_id:
            _logger.warning("⚠️ No hay orden de venta Odoo para reservar stock: TN Order ID %s", self.tn_order_id)
            return False

        # Buscar pickings de entrega relacionados con la orden de venta
        pickings = self.env['stock.picking'].search([
            ('sale_id', '=', self.odoo_sale_order_id.id),
            ('picking_type_id.code', '=', 'outgoing'),  # Solo pickings de salida
            ('state', 'in', ['draft', 'waiting', 'confirmed', 'assigned']),  # Estados que pueden reservar
        ])

        if not pickings:
            _logger.warning("⚠️ No se encontraron pickings para reservar stock: Orden Odoo %s", self.odoo_sale_order_id.name)
            return False

        reserved_count = 0
        for picking in pickings:
            try:
                # Si el picking está en draft, confirmarlo primero
                if picking.state == 'draft':
                    picking.action_confirm()
                
                # Reservar stock (action_assign)
                if picking.state in ['waiting', 'confirmed']:
                    picking.action_assign()
                    reserved_count += 1
                    _logger.info("✅ Stock reservado en picking: %s (Orden: %s)", picking.name, self.odoo_sale_order_id.name)
                elif picking.state == 'assigned':
                    # Ya está reservado
                    reserved_count += 1
                    _logger.info("✅ Stock ya estaba reservado en picking: %s", picking.name)
            except Exception as e:
                _logger.exception("❌ Error reservando stock en picking %s: %s", picking.name, e)
                continue

        if reserved_count > 0:
            _logger.info("✅ Stock reservado en %d picking(s) para orden %s", reserved_count, self.odoo_sale_order_id.name)
            return True
        else:
            _logger.warning("⚠️ No se pudo reservar stock en ningún picking para orden %s", self.odoo_sale_order_id.name)
            return False

    def _sync_odoo_on_tn_cancelled(self):
        """
        Cuando TiendaNube cancela la orden (status=cancelled en tn.sale.order):
        misma lógica que mercadolibre_connector en ml.sale.
        """
        for rec in self:
            sale = rec.odoo_sale_order_id
            if not sale:
                continue
            lines = []
            if sale.state == 'cancel':
                lines.append(_('Orden de venta Odoo %s ya estaba cancelada.') % sale.name)
                rec._tn_cancel_post_activity(sale, lines)
                continue

            pickings = sale.picking_ids
            if any(p.state == 'done' for p in pickings):
                lines.append(
                    _('Pickings: hay al menos una transferencia en estado Hecho (done). '
                      'No se cancela automáticamente la venta ni las facturas; gestionar devolución manualmente.')
                )
                rec._tn_cancel_post_activity(sale, lines)
                continue

            for picking in pickings:
                if picking.state in ('cancel', 'done'):
                    continue
                if picking.state in ('draft', 'waiting', 'confirmed', 'assigned'):
                    try:
                        picking.action_cancel()
                        lines.append(_('Picking %s cancelado.') % (picking.name or str(picking.id)))
                    except Exception as err:
                        lines.append(
                            _('Picking %s: error al cancelar: %s')
                            % (picking.name or str(picking.id), err)
                        )
                        _logger.exception(
                            'Error cancelando picking %s para tn.sale.order %s',
                            picking.id, rec.tn_order_id,
                        )
                else:
                    lines.append(
                        _('Picking %s: estado %s no tratado automáticamente.')
                        % (picking.name or str(picking.id), picking.state)
                    )

            lines.extend(rec._tn_cancel_handle_customer_invoices(sale))

            if sale.state != 'cancel':
                try:
                    sale.action_cancel()
                    lines.append(_('Orden de venta %s cancelada.') % sale.name)
                except Exception as err:
                    lines.append(_('Error al cancelar la orden de venta: %s') % err)
                    _logger.exception(
                        'Error en action_cancel para sale.order %s (tn.sale.order %s)',
                        sale.id, rec.tn_order_id,
                    )
            else:
                lines.append(_('Orden de venta %s quedó cancelada.') % sale.name)

            rec._tn_cancel_post_activity(sale, lines)

    def _tn_cancel_handle_customer_invoices(self, sale_order):
        """Facturas de cliente: borrador → cancelar; publicada → NC en borrador (sin publicar)."""
        self.ensure_one()
        lines = []
        invoices = sale_order.invoice_ids.filtered(
            lambda m: m.move_type == 'out_invoice' and m.state != 'cancel'
        )
        for inv in invoices:
            if inv.state == 'draft':
                try:
                    cancel_draft = getattr(inv, 'button_cancel', None) or getattr(inv, 'action_cancel', None)
                    if not cancel_draft:
                        raise ValueError('no draft cancel method on account.move')
                    cancel_draft()
                    lines.append(_('Factura borrador cancelada: %s.') % (inv.name or str(inv.id)))
                except Exception as err:
                    lines.append(
                        _('Factura borrador %s: no se pudo cancelar: %s')
                        % (inv.name or str(inv.id), err)
                    )
                    _logger.exception(
                        'Error button_cancel factura borrador %s (tn.sale.order %s)',
                        inv.id, self.tn_order_id,
                    )
            elif inv.state == 'posted':
                try:
                    reversals = inv._reverse_moves(cancel=False)
                    for rev in reversals:
                        if rev.state == 'posted':
                            lines.append(
                                _('Nota de crédito %s quedó publicada (revisar; se esperaba borrador).')
                                % (rev.name or str(rev.id))
                            )
                        else:
                            lines.append(
                                _('Nota de crédito en borrador creada: %s.')
                                % (rev.name or str(rev.id))
                            )
                except Exception as err:
                    lines.append(
                        _('Factura publicada %s: error al generar nota de crédito: %s')
                        % (inv.name or str(inv.id), err)
                    )
                    _logger.exception(
                        'Error _reverse_moves para factura %s (tn.sale.order %s)',
                        inv.id, self.tn_order_id,
                    )
        return lines

    def _tn_cancel_post_activity(self, sale_order, summary_lines):
        """Actividad en la orden de venta con resumen de la cancelación TN."""
        self.ensure_one()
        if not summary_lines:
            summary_lines = [
                _('Venta TiendaNube %s marcada como cancelada.') % self.tn_order_id
            ]
        note = '\n'.join(summary_lines)
        try:
            todo_type = self.env.ref('mail.mail_activity_data_todo')
            assign_user = sale_order.user_id or sale_order.create_uid or self.env.user
            sale_order.activity_schedule(
                activity_type_id=todo_type.id,
                user_id=assign_user.id,
                summary=_('TiendaNube: orden TN cancelada (%s)') % self.tn_order_id,
                note=note,
            )
        except Exception as err:
            _logger.warning(
                'No se pudo crear actividad en sale.order %s (tn.sale.order %s): %s',
                sale_order.id, self.tn_order_id, err,
            )

    def write(self, vals):
        """
        Sobrescribir write para procesar estado de envío automáticamente cuando cambie.
        También sincroniza el estado a TiendaNube si cambió desde Odoo.
        Si status pasa a cancelled, alinea Odoo como en mercadolibre_connector.
        """
        becoming_cancelled = self.env['tn.sale.order']
        if vals.get('status') == 'cancelled':
            becoming_cancelled = self.filtered(lambda r: r.status != 'cancelled')

        # Si se está estableciendo syncing_to_tn explícitamente, no procesar sincronización
        is_syncing = vals.get('syncing_to_tn', False)
        # Si solo se está estableciendo odoo_sale_order_id (vinculación), no procesar sincronización
        is_linking = 'odoo_sale_order_id' in vals and len(vals) == 1
        
        result = super(TNSaleOrder, self).write(vals)

        if becoming_cancelled:
            becoming_cancelled._sync_odoo_on_tn_cancelled()
        
        # Si solo se está vinculando la orden, no hacer nada más
        if is_linking:
            return result
        
        # Si el estado de envío cambió (p. ej. desde Odoo), solo sincronizar a TN; no disparar reserva/validación desde TN
        if 'fulfillment_status' in vals:
            new_status = vals.get('fulfillment_status')
            for record in self:
                # Sincronizar a TiendaNube solo si:
                # 1. No estamos en proceso de sincronización (syncing_to_tn no está en True)
                # 2. Tiene tn_order_id
                # 3. El cambio no viene del webhook (syncing_to_tn está en False o no se estableció)
                should_sync = (
                    not is_syncing and  # No estamos sincronizando
                    record.tn_order_id and  # Tiene ID de TiendaNube
                    not record.syncing_to_tn  # No está marcado como sincronizando
                )
                
                if should_sync:
                    try:
                        record._sync_fulfillment_status_to_tn(new_status)
                    except Exception as e:
                        _logger.exception("❌ Error sincronizando estado de envío a TiendaNube: %s", e)
        
        return result

    def _sync_fulfillment_status_to_tn(self, fulfillment_status):
        """
        Sincroniza el estado de fulfillment a TiendaNube.
        
        Args:
            fulfillment_status: Estado de fulfillment a sincronizar ('to_pack', 'to_ship', 'shipped')
        """
        self.ensure_one()
        
        _logger.info("=" * 80)
        _logger.info("🔄 INICIO: Sincronización de estado de envío a TiendaNube")
        _logger.info("📦 Orden TiendaNube: TN Order ID %s (Odoo ID: %s)", self.tn_order_id, self.id)
        _logger.info("📊 Estado actual en Odoo: %s", self.fulfillment_status)
        _logger.info("📤 Estado a sincronizar: %s", fulfillment_status)
        
        # Mapeo de estados Odoo -> TiendaNube
        status_map = {
            'to_pack': 'unfulfilled',
            'to_ship': 'fulfilled',
            'shipped': 'shipped',
        }
        tn_status = status_map.get(fulfillment_status, 'unfulfilled')
        _logger.info("🗺️  Mapeo de estado: '%s' -> '%s' (TiendaNube)", fulfillment_status, tn_status)
        
        if not self.tn_order_id:
            _logger.warning("⚠️ No hay ID de orden TiendaNube para sincronizar: %s", self.name)
            _logger.info("=" * 80)
            return False
        
        try:
            # Marcar que estamos sincronizando para evitar loops
            _logger.info("🔒 Marcando syncing_to_tn = True para evitar loops")
            self.syncing_to_tn = True
            
            # Obtener servicio de sincronización
            sync_service = self.env['tn.sync.service']
            _logger.info("📡 Llamando a update_order_fulfillment_status en TiendaNube API")
            
            # Actualizar en TiendaNube
            result = sync_service.update_order_fulfillment_status(
                self.tn_order_id,
                fulfillment_status
            )
            
            _logger.info("📥 Respuesta de TiendaNube API: %s", result)
            
            if result.get('success'):
                _logger.info("✅ Estado de envío sincronizado exitosamente a TiendaNube")
                _logger.info("   - Orden TiendaNube: %s", self.tn_order_id)
                _logger.info("   - Estado Odoo: %s", fulfillment_status)
                _logger.info("   - Estado TiendaNube: %s", tn_status)
                _logger.info("=" * 80)
                return True
            else:
                error_msg = result.get('error', 'Error desconocido')
                _logger.warning("⚠️ No se pudo sincronizar estado de envío a TiendaNube")
                _logger.warning("   - Error: %s", error_msg)
                _logger.warning("   - Respuesta completa: %s", result)
                _logger.info("=" * 80)
                return False
                
        except Exception as e:
            _logger.exception("❌ Error sincronizando estado de envío a TiendaNube: %s", e)
            _logger.info("=" * 80)
            return False
        finally:
            # Resetear el flag
            _logger.info("🔓 Reseteando syncing_to_tn = False")
            self.syncing_to_tn = False
    
    def _mark_fulfillment_order_as_delivered(self):
        """
        Marca el fulfillment order de esta orden como 'delivered' (entregado) en TiendaNube.
        Usa la API de Fulfillment Orders según la documentación oficial.
        """
        self.ensure_one()
        
        if not self.tn_order_id:
            _logger.warning("⚠️ No hay ID de orden TiendaNube para marcar como entregado: %s", self.name)
            return False
        
        try:
            _logger.info("=" * 80)
            _logger.info("📦 INICIO: Marcando fulfillment order como 'delivered' en TiendaNube")
            _logger.info("📦 Orden TiendaNube: TN Order ID %s (Odoo ID: %s)", self.tn_order_id, self.id)
            
            # Obtener servicio de sincronización
            sync_service = self.env['tn.sync.service']
            
            # Marcar fulfillment order como 'delivered'
            result = sync_service.mark_fulfillment_order_as_delivered(self.tn_order_id)
            
            if result.get('success'):
                updated_count = result.get('updated_count', 0)
                _logger.info("✅ Fulfillment order(s) marcado(s) como 'delivered' exitosamente")
                _logger.info("   - Orden TiendaNube: %s", self.tn_order_id)
                _logger.info("   - Fulfillment orders actualizados: %d", updated_count)
                _logger.info("=" * 80)
                return True
            else:
                error_msg = result.get('error', 'Error desconocido')
                _logger.warning("⚠️ No se pudo marcar fulfillment order como 'delivered'")
                _logger.warning("   - Error: %s", error_msg)
                _logger.warning("   - Respuesta completa: %s", result)
                _logger.info("=" * 80)
                return False
                
        except Exception as e:
            _logger.exception("❌ Error marcando fulfillment order como 'delivered' en TiendaNube: %s", e)
            _logger.info("=" * 80)
            return False

    def _create_invoice_from_sale_order(self, sale_order, config):
        """
        Crea la factura desde la orden de venta usando la acción con ID 354.
        """
        self.ensure_one()

        # Obtener diario
        # Obtener configuración (usar la configuración específica si está disponible)
        config = self.tn_config_id if self.tn_config_id else self.env['tn.config'].get_config()
        journal = config.invoice_journal_id
        if not journal:
            # Buscar diario de ventas por defecto
            journal = self.env['account.journal'].search([
                ('type', '=', 'sale'),
                ('company_id', '=', self.env.company.id)
            ], limit=1)
            if not journal:
                _logger.warning("⚠️ No se encontró diario de ventas. No se creará factura.")
                return False

        # Crear factura directamente desde la orden de venta
        # El método _create_invoices() devuelve un recordset
        invoices = sale_order._create_invoices()
        
        if invoices:
            # Tomar la primera factura (normalmente solo hay una)
            invoice = invoices[0] if len(invoices) > 0 else invoices
            
            # Configurar tipo de factura y diario
            invoice.write({
                'journal_id': journal.id,
                'move_type': 'out_invoice',
            })

            # Factura siempre a Consumidor Final.
            if config.consumidor_final_partner_id:
                invoice.write({'partner_id': config.consumidor_final_partner_id.id})
                _logger.info(
                    '✅ Factura: partner_id = Consumidor Final (%s)',
                    config.consumidor_final_partner_id.name,
                )

            # Reemplazar cuenta de ingresos de líneas de producto si hay una cuenta configurada.
            income_account = config.default_income_account_id
            if income_account:
                invoice_lines = invoice.invoice_line_ids.filtered(lambda l: not l.display_type)
                if invoice_lines:
                    invoice_lines.write({'account_id': income_account.id})
                    _logger.info(
                        "✅ Cuenta de ingresos aplicada a factura %s: %s",
                        invoice.name or invoice.id,
                        income_account.display_name
                    )
            
            # Validar y publicar la factura
            invoice.action_post()
            _logger.info("✅ Factura creada y publicada: %s (Diario: %s)", invoice.name, journal.name)
            return invoice
        else:
            _logger.warning("⚠️ No se pudo crear factura para orden de venta: %s", sale_order.name)
            return False

    def _map_payment_status(self, order_data, mapped_status=None):
        """Mapea estado de pago desde payload de TiendaNube."""
        payment_status_raw = str(order_data.get('payment_status') or '').lower()
        status_raw = str(mapped_status or order_data.get('status') or '').lower()

        if payment_status_raw == 'paid' or status_raw == 'paid':
            return 'paid'
        if payment_status_raw in ('refunded', 'partially_refunded') or status_raw in ('refunded', 'partially_refunded'):
            return 'refunded'
        return 'pending'

    def _get_existing_invoice_for_sale_order(self, sale_order):
        """Obtiene la factura de cliente vigente para la SO (si existe)."""
        invoices = sale_order.invoice_ids.filtered(
            lambda inv: inv.move_type == 'out_invoice' and inv.state != 'cancel'
        )
        if not invoices:
            return False
        posted = invoices.filtered(lambda inv: inv.state == 'posted')
        return (posted.sorted(lambda inv: inv.id, reverse=True)[:1] or invoices.sorted(lambda inv: inv.id, reverse=True)[:1])[:1]

    def _ensure_posted_invoice_for_sale_order(self, sale_order, config=None):
        """
        Asegura una factura publicada:
        - reutiliza factura existente (y publica si está draft)
        - crea una nueva si no existe
        """
        self.ensure_one()
        existing = self._get_existing_invoice_for_sale_order(sale_order)
        invoice = existing[0] if existing else False
        if invoice:
            if invoice.state == 'draft':
                invoice.action_post()
                _logger.info("✅ Factura borrador publicada para orden %s: %s", sale_order.name, invoice.name)
            return invoice
        return self._create_invoice_from_sale_order(sale_order, config)

    def _process_payment_state_from_tn(self, tn_config=None):
        """
        Reglas solicitadas para TiendaNube:
        - status=payment_required -> crear/publicar factura sin registrar pago.
        - payment_status=paid -> crear/publicar factura sin registrar pago.
        """
        self.ensure_one()
        sale_order = self.odoo_sale_order_id
        if not sale_order:
            _logger.info("ℹ️ Orden TN %s sin SO Odoo vinculada; se omite lógica de pago.", self.tn_order_id)
            return False

        config = tn_config or self.tn_config_id or self.env['tn.config'].get_config()
        if not config or not config.auto_invoice:
            _logger.info(
                "ℹ️ auto_invoice deshabilitado para TN Order %s; no se crea/publica factura automática.",
                self.tn_order_id
            )
            return False

        requires_invoice = self.status == 'payment_required' or self.payment_status in ('paid', 'refunded')
        if not requires_invoice:
            return False

        invoice = self._ensure_posted_invoice_for_sale_order(sale_order, config)
        if not invoice:
            return False

        _logger.info(
            "ℹ️ Orden TN %s en estado '%s'/%s: factura creada/publicada sin registrar pago automatico.",
            self.tn_order_id, self.status, self.payment_status
        )
        return True

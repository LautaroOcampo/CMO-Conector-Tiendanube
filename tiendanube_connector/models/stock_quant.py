# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
import logging

_logger = logging.getLogger(__name__)


def _schedule_tn_sync_after_commit(env, product_id, location_id):
    """
    Programa la sincronización con TiendaNube para después del commit.
    Evita ejecutar la sync dentro de la misma transacción que aplica el inventario,
    lo que podría abortar la transacción (p. ej. InFailedSqlTransaction) si la sync
    falla o dispara más escrituras.
    """
    if not product_id:
        return

    def _run_after_commit():
        try:
            env['stock.quant']._sync_stock_to_tiendanube(int(product_id), int(location_id) if location_id else None)
        except Exception as e:
            _logger.exception("❌ Error sincronizando stock a TiendaNube (post-commit): %s", e)

    try:
        if hasattr(env.cr, 'postcommit') and hasattr(env.cr.postcommit, 'add'):
            env.cr.postcommit.add(_run_after_commit)
        else:
            _logger.debug("postcommit no disponible; omitiendo sync TN para product_id=%s", product_id)
    except Exception as e:
        _logger.warning("No se pudo programar sync TN post-commit: %s", e)


def _schedule_tn_min_stock_check_after_commit(env, product_id, location_id):
    """
    Programa la verificación de stock mínimo (pausar/activar publicación) para después del commit.
    Se dispara en cualquier movimiento de stock (compra, venta, movimiento) cuando hay
    control de stock mínimo activado (global o por publicación).
    """
    if not product_id:
        return

    def _run_after_commit():
        try:
            env['stock.quant']._check_min_stock_pause_unpause_for_product(
                int(product_id), int(location_id) if location_id else None
            )
        except Exception as e:
            _logger.exception("❌ Error en verificación de stock mínimo TN (post-commit): %s", e)

    try:
        if hasattr(env.cr, 'postcommit') and hasattr(env.cr.postcommit, 'add'):
            env.cr.postcommit.add(_run_after_commit)
        else:
            _logger.debug("postcommit no disponible; omitiendo check min-stock TN para product_id=%s", product_id)
    except Exception as e:
        _logger.warning("No se pudo programar check min-stock TN post-commit: %s", e)


def _schedule_tn_after_stock_change(env, product_id, location_id, do_sync_stock, do_min_stock_check):
    """
    Programa una única callback post-commit para sync de stock y/o verificación de stock mínimo.
    - do_sync_stock: sincronizar stock a TN (auto_sync_stock_on_odoo_change).
    - do_min_stock_check: verificar pausa/activación por stock mínimo (enable_min_stock_pause/unpause).
    Si ambos están activos, solo se ejecuta sync (que ya incluye la verificación de min-stock).
    """
    if not product_id:
        return
    pid, loc = int(product_id), int(location_id) if location_id else None

    def _run():
        try:
            if do_sync_stock:
                env['stock.quant']._sync_stock_to_tiendanube(pid, loc)
            elif do_min_stock_check:
                env['stock.quant']._check_min_stock_pause_unpause_for_product(pid, loc)
        except Exception as e:
            _logger.exception("❌ Error TN post-commit (product_id=%s): %s", pid, e)

    try:
        if hasattr(env.cr, 'postcommit') and hasattr(env.cr.postcommit, 'add'):
            env.cr.postcommit.add(_run)
        else:
            _logger.debug("postcommit no disponible; product_id=%s", pid)
    except Exception as e:
        _logger.warning("No se pudo programar TN post-commit: %s", e)


class StockQuant(models.Model):
    """Extensión de stock.quant para sincronizar stock automáticamente con TiendaNube"""
    _inherit = 'stock.quant'

    def write(self, vals):
        """
        Sobrescribir write para sincronizar stock automáticamente a TiendaNube cuando cambia en Odoo.
        La sincronización se agenda para después del commit para no abortar la transacción de stock.
        """
        result = super(StockQuant, self).write(vals)

        # Cambios físicos o reservas (ambos afectan virtual_available / stock esperado)
        if 'quantity' in vals or 'reserved_quantity' in vals:
            seen = set()
            configs_sync = self.env['tn.config'].search([('auto_sync_stock_on_odoo_change', '=', True)])
            configs_min = self.env['tn.config'].search([
                '|', ('enable_min_stock_pause', '=', True), ('enable_min_stock_unpause', '=', True)
            ])
            for rec in self:
                product_id = (rec.product_id.id if rec.product_id else None) or vals.get('product_id')
                location_id = (rec.location_id.id if rec.location_id else None) or vals.get('location_id')
                if not product_id or (product_id, location_id or 0) in seen:
                    continue
                seen.add((product_id, location_id or 0))
                if configs_sync or configs_min:
                    _schedule_tn_after_stock_change(
                        self.env, product_id, location_id,
                        do_sync_stock=bool(configs_sync),
                        do_min_stock_check=bool(configs_min) and not configs_sync
                    )

        return result

    @api.model_create_multi
    def create(self, vals_list):
        """
        Sobrescribir create para sincronizar stock automáticamente a TiendaNube cuando se crea en Odoo.
        La sincronización se agenda para después del commit para no abortar la transacción de stock.
        """
        result = super(StockQuant, self).create(vals_list)

        configs_sync = self.env['tn.config'].search([('auto_sync_stock_on_odoo_change', '=', True)])
        configs_min = self.env['tn.config'].search([
            '|', ('enable_min_stock_pause', '=', True), ('enable_min_stock_unpause', '=', True)
        ])
        seen = set()
        for record in result:
            if not record.product_id or (record.product_id.id, record.location_id.id or 0) in seen:
                continue
            seen.add((record.product_id.id, record.location_id.id or 0))
            if configs_sync or configs_min:
                _schedule_tn_after_stock_change(
                    self.env, record.product_id.id, record.location_id.id,
                    do_sync_stock=bool(configs_sync),
                    do_min_stock_check=bool(configs_min) and not configs_sync
                )

        return result
    
    @api.model
    def _sync_stock_to_tiendanube(self, product_id, location_id=None):
        """
        Sincroniza el stock de un producto a TiendaNube cuando cambia en Odoo.
        
        Args:
            product_id: int - ID del producto cuyo stock cambió
            location_id: int - ID de la ubicación donde cambió el stock (opcional)
        """
        try:
            # Obtener el producto
            product = self.env['product.product'].browse(product_id)
            if not product.exists():
                _logger.warning("⚠️ Producto ID %s no existe", product_id)
                return
            
            location = None
            if location_id:
                location = self.env['stock.location'].browse(location_id)
                if not location.exists():
                    location = None
            
            _logger.info("=" * 80)
            _logger.info("🔄 INICIO: Sincronización automática de stock a TiendaNube")
            _logger.info("📦 Producto: %s (ID: %s)", product.name, product.id)
            _logger.info("📍 Ubicación: %s (ID: %s)", location.name if location else 'N/A', location.id if location else 'N/A')
            
            # Verificar si hay alguna configuración con sincronización automática activada
            configs = self.env['tn.config'].search([
                ('auto_sync_stock_on_odoo_change', '=', True)
            ])
            
            if not configs:
                _logger.info("ℹ️ Sincronización automática de stock desde Odoo desactivada. No se sincroniza.")
                _logger.info("=" * 80)
                return
            
            # Usar la primera configuración encontrada (o la del producto si tiene tn_config_id)
            config = configs[0]
            
            # Obtener la ubicación de stock del almacén configurado
            stock_location = None
            if config.warehouse_id:
                # Usar la ubicación de stock del almacén (lot_stock_id)
                stock_location = config.warehouse_id.lot_stock_id
                _logger.info("🏭 Usando almacén configurado: %s (ID: %s)", config.warehouse_id.name, config.warehouse_id.id)
                _logger.info("📍 Ubicación de stock del almacén: %s (ID: %s)", stock_location.name if stock_location else 'N/A', stock_location.id if stock_location else 'N/A')
            elif config.stock_location_id:
                # Fallback: usar stock_location_id si warehouse_id no está configurado
                stock_location = config.stock_location_id
                _logger.info("📍 Usando ubicación de stock configurada directamente: %s (ID: %s)", stock_location.name, stock_location.id)
            
            if stock_location:
                if location:
                    # Verificar si la ubicación es la configurada o es hija de ella
                    # Usar child_of para verificar jerarquía de ubicaciones
                    location_ids = self.env['stock.location'].search([
                        ('id', 'child_of', stock_location.id)
                    ]).ids
                    
                    _logger.info("🔍 Verificando ubicación: %s (ID: %s) contra almacén: %s (ID: %s)", 
                               location.name, location.id, stock_location.name, stock_location.id)
                    _logger.info("   Ubicaciones válidas (hijas del almacén): %s", location_ids)
                    
                    if location.id not in location_ids:
                        _logger.warning("⚠️ La ubicación %s (ID: %s) NO corresponde al almacén configurado %s (ID: %s).", 
                                       location.name, location.id, stock_location.name, stock_location.id)
                        _logger.warning("   ⚠️ NO se sincronizará el stock para este cambio.")
                        _logger.info("=" * 80)
                        return
                    else:
                        _logger.info("✅ Ubicación %s corresponde al almacén configurado %s", 
                                   location.name, stock_location.name)
                else:
                    # Si no hay location específica, sincronizar de todas formas
                    _logger.info("ℹ️ No hay ubicación específica, sincronizando de todas formas")
            else:
                _logger.warning("⚠️ No hay almacén configurado en tn.config. Sincronizando de todas formas.")
            
            # Buscar publicaciones de TiendaNube relacionadas con este producto
            # Solo de configuraciones con sincronización automática activada
            product_template = product.product_tmpl_id
            
            publications = self.env['tn.publication'].search([
                ('odoo_product_id', '=', product_template.id),
                ('tn_product_id', '!=', False),  # Solo publicaciones ya sincronizadas
                ('active', '=', True),
                ('tn_config_id', 'in', configs.ids),  # Solo publicaciones de configuraciones con sincronización activada
            ])

            if not publications:
                _logger.warning("⚠️ No se encontraron publicaciones de TiendaNube para producto %s (Template ID: %s)", 
                               product.name, product_template.id)
                _logger.warning("   Verificar que:")
                _logger.warning("   1. El producto tenga una publicación de TiendaNube relacionada")
                _logger.warning("   2. La publicación tenga tn_product_id (ya sincronizada)")
                _logger.warning("   3. La publicación esté activa")
                _logger.info("=" * 80)
                return
            
            _logger.info("📦 Publicaciones encontradas: %d", len(publications))
            
            # Para cada publicación, actualizar el stock de las variantes relacionadas
            for publication in publications:
                try:
                    _logger.info("🔄 Sincronizando stock para publicación: %s (ID TN: %s)", 
                               publication.title, publication.tn_product_id)
                    
                    # Actualizar stock desde Odoo primero
                    # IMPORTANTE: Esto actualiza el stock con el valor ABSOLUTO actual desde Odoo,
                    # no suma ni resta. Se reemplaza el valor anterior con el valor actual.
                    _logger.info("📊 Actualizando stock desde Odoo para publicación %s...", publication.title)
                    _logger.info("   ℹ️ Se actualizará con el valor ABSOLUTO del stock actual en Odoo (no se suma ni resta)")
                    publication._sync_stock_and_price_from_odoo()
                    
                    # Verificar que las variantes tengan stock actualizado
                    _logger.info("📦 Stock de variantes después de actualizar desde Odoo:")
                    for variant in publication.variant_ids:
                        if variant.odoo_variant_id:
                            _logger.info("   - Variante %s (TN ID: %s): stock=%s, odoo_variant_id=%s", 
                                       variant.name or 'sin nombre', variant.tn_variant_id, variant.stock, variant.odoo_variant_id.id)
                    
                    # Sincronizar solo el stock (no el precio) a TiendaNube
                    sync_service = self.env['tn.sync.service']
                    if publication.tn_product_id:
                        _logger.info("📤 Enviando actualización de stock a TiendaNube (product_id: %s)...", publication.tn_product_id)
                        try:
                            # Sincronizar solo stock, no precio
                            sync_service._update_variants_individually(publication, publication.tn_product_id, sync_stock=True, sync_price=False)
                            _logger.info("✅ Stock sincronizado exitosamente a TiendaNube para publicación %s", publication.title)
                            
                            # Verificar y actualizar estado de publicación según stock mínimo
                            _logger.info("🔍 Llamando a _check_and_update_published_status para publicación '%s' (desde stock_quant)...", publication.title)
                            try:
                                publication._check_and_update_published_status()
                            except Exception as e:
                                _logger.exception("⚠️ Error verificando estado de publicación según stock mínimo: %s", e)
                                # Continuar aunque falle la verificación
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
            _logger.exception("❌ Error en sincronización automática de stock a TiendaNube: %s", e)

    @api.model
    def _check_min_stock_pause_unpause_for_product(self, product_id, location_id=None):
        """
        Verificación de stock mínimo para pausar/activar publicaciones en TN.
        Se llama cuando hay movimiento de stock (compra, venta, movimiento) y alguna
        configuración tiene enable_min_stock_pause o enable_min_stock_unpause activado.
        Actualiza el stock en las publicaciones desde Odoo y aplica las reglas de pausa/despausa.
        """
        try:
            product = self.env['product.product'].browse(product_id)
            if not product.exists():
                _logger.warning("⚠️ _check_min_stock_pause_unpause: producto ID %s no existe", product_id)
                return

            configs = self.env['tn.config'].search([
                '|', ('enable_min_stock_pause', '=', True), ('enable_min_stock_unpause', '=', True)
            ])
            if not configs:
                _logger.debug("ℹ️ _check_min_stock_pause_unpause: ninguna config con control de stock mínimo")
                return

            product_template = product.product_tmpl_id
            publications = self.env['tn.publication'].search([
                ('odoo_product_id', '=', product_template.id),
                ('tn_product_id', '!=', False),
                ('active', '=', True),
                ('tn_config_id', 'in', configs.ids),
            ])
            if not publications:
                _logger.debug("ℹ️ _check_min_stock_pause_unpause: no hay publicaciones TN para producto %s", product.name)
                return

            location = self.env['stock.location'].browse(location_id) if location_id else None
            for publication in publications:
                config = publication.tn_config_id
                stock_location = None
                if config.warehouse_id and config.warehouse_id.lot_stock_id:
                    stock_location = config.warehouse_id.lot_stock_id
                elif config.stock_location_id:
                    stock_location = config.stock_location_id
                if stock_location and location:
                    location_ids = self.env['stock.location'].search([
                        ('id', 'child_of', stock_location.id)
                    ]).ids
                    if location.id not in location_ids:
                        _logger.debug("ℹ️ _check_min_stock_pause_unpause: ubicación %s no aplica para pub %s",
                                     location.name, publication.title)
                        continue
                try:
                    publication._sync_stock_and_price_from_odoo()
                    publication._check_and_update_published_status()
                except Exception as e:
                    _logger.exception("❌ _check_min_stock_pause_unpause para publicación '%s': %s", publication.title, e)
        except Exception as e:
            _logger.exception("❌ Error en _check_min_stock_pause_unpause_for_product: %s", e)

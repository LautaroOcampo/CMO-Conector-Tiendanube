# TiendaNube Connector (Odoo)

Conector para sincronizar productos, stock, precios y órdenes entre Odoo y TiendaNube.

---

## Requisitos

- Odoo 17 (o compatible)
- Cuenta en TiendaNube y aplicación en el [panel de socios tecnológicos](https://partners.tiendanube.com/)
- Módulos Odoo: `base`, `product`, `sale`, `stock`, `mail`, `account`

---

## Instalación

1. Instalar el módulo **TiendaNube Connector** desde Apps.
2. Ir a **Configuración** (o menú de la app) y abrir **Configuración TiendaNube**.
3. Crear una configuración con **Nombre**, **Client ID**, **Client Secret** y **Store ID**.
4. Pulsar **Autorizar Aplicación** para obtener Access Token y Refresh Token.
5. Opcional: configurar almacén, lista de precios, webhook y polling según se indica más abajo.

---

## Configuración de la cuenta

- **Credenciales:** Client ID, Client Secret (desde TiendaNube). Store ID = ID de la tienda.
- **Autorizar aplicación:** obtiene Access Token (y Refresh Token si aplica). El token no caduca por tiempo; solo al desinstalar la app o regenerar en TiendaNube.
- **Configuración del stock:** almacén de Odoo, tipo de stock (disponible/esperado), lista de precios para sincronizar precios.
- **Configuración de ventas:** lista de precios para ventas, impuesto por defecto, almacén de envíos, facturación automática.
- **Webhook de ventas:** se crea automáticamente al guardar con token y Store ID. Opción “Procesar Webhooks” para activar/desactivar el procesamiento (el webhook en TN sigue activo).
- **Sincronización de órdenes (polling):**
  - **Activar polling:** el cron sincroniza órdenes cada 10 minutos.
  - **Sincronizar órdenes ahora:** botón para ejecutar la sincronización manualmente.
  - **Estado del cron:** próxima ejecución, última ejecución y si el cron está activo (acción planificada global).
  - **Máximo días hacia atrás**, **órdenes por página** y **máximo de páginas** para limitar la búsqueda y el volumen por ejecución.
- **Sincronización automática desde Odoo:** opciones para sincronizar stock y/o precio a TiendaNube cuando cambien en Odoo.
- **Control de stock mínimo:** pausar/despausar publicaciones en TN según umbrales de stock (global y por publicación).

---

## Uso habitual

1. **Productos y publicaciones**
   - Crear o importar productos en Odoo.
   - Desde **Publicaciones TiendaNube**, usar el asistente para crear publicaciones a partir de productos Odoo (elegir cuenta TN).
   - Exportar cada publicación a TiendaNube (la publicación debe tener `tn_product_id`). Después se puede sincronizar stock y precio.

2. **Stock y precios**
   - Sincronización manual: en la publicación, **Sincronizar Stock y Precio** (o a nivel de cuenta **Sincronizar Todos los Stocks y Precios**).
   - Si está activada la sincronización automática desde Odoo, los cambios de stock/precio en Odoo se envían a TN.

3. **Órdenes desde TiendaNube**
   - **Webhook:** TN envía avisos; el conector los registra. El procesamiento real de órdenes se hace por polling.
   - **Polling:** el cron (cada 10 min) consulta la API y crea/actualiza órdenes en Odoo. Sin duplicados (idempotencia por `tn_order_id`).
   - **Sincronizar órdenes ahora:** en la configuración de la cuenta, para forzar una sincronización inmediata.

4. **Estado del cron**
   - En la misma configuración de la cuenta, en la sección “Sincronización de Órdenes (Polling)” se muestran la próxima y la última ejecución del cron y si está activo. El cron global está en **Configuración > Técnico > Automatización > Acciones planificadas** (“Sincronizar Órdenes de TiendaNube (Polling)”).

---

## Documentación técnica

- API TiendaNube: [api-documentation](https://tiendanube.github.io/api-documentation/)
- Rate limit por defecto: 2 req/s (120/min). Plan Next: 20 req/s (1200/min). El conector reacciona al 429 (Reintentar después).
- Variantes: si una publicación ya está exportada pero las variantes en Odoo no tienen ID de TN, al sincronizar stock/precio se intenta obtener y rellenar esos IDs desde la API (por SKU o posición).

---

## Soporte

Autor: Galarreta · galarreta.co

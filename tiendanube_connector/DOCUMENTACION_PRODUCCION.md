# TiendaNube Connector – Errores, Recomendaciones y Checklist Producción

---

## 1. Errores encontrados y correcciones aplicadas

### 1.1 Multi-compañía en polling (corregido)
- **Problema:** En `sync_orders_by_polling` se usaba `self.env.company.id` para buscar y crear órdenes. Cuando el cron corre no hay usuario activo, así que `env.company` puede ser la primera compañía de la BD. Con varias cuentas TN en distintas compañías, las órdenes podían crearse o buscarse en la compañía incorrecta.
- **Corrección:** 
  - Búsqueda de orden existente: usar `self.company_id.id` (compañía de la configuración).
  - Creación/actualización: pasar `allowed_company_ids=[self.company_id.id]` en el contexto para que `create_order_from_webhook` trabaje en la compañía correcta.
  - En `create_order_from_webhook`: asignar `company_id` de la orden desde `tn_config.company_id` cuando exista configuración.


### 1.2 Constraint SQL en `tn.webhook.notification`
- **Detalle:** El constraint usa `date_received::date` (cast a fecha). Es sintaxis **PostgreSQL**. Odoo 
en producción suele usar PostgreSQL, así que es válido. Si en algún momento se usara otro motor (MySQL, etc.), ese constraint fallaría en la migración.
- **Recomendación:** Mantener PostgreSQL en producción. Si se requiere compatibilidad con otros DB, habría que cambiar a un campo de fecha calculado o a un constraint sin `::date`.


### 1.3 Webhook sin compañía explícita
- **Detalle:** Al guardar la notificación con `create_notification`, se usa `self.env.company.id`. El endpoint del webhook es `auth='public'`; si no hay sesión, `env.company` puede ser la compañía por defecto. Con una sola compañía no hay problema; con varias, no se puede saber a qué tienda TN pertenece el webhook sin más datos (p. ej. store_id en el payload).
- **Recomendación:** Si usas multi-compañía y varias cuentas TN, valorar incluir en el webhook un identificador (store_id o similar) y mapearlo a `tn.config` para asignar `company_id` (y `tn_config_id`) correctamente.

### 1.4 Posible atributo `lastcall` en `ir.cron`
- **Detalle:** En `_compute_cron_orders_status` se usa `cron.lastcall if hasattr(cron, 'lastcall')`. En versiones estándar de Odoo el campo existe; si en alguna personalización se eliminara o renombrara, el `hasattr` evita el fallo.
- **Estado:** Cubierto con comprobación defensiva.

---

## 2. Flujo de importación de ventas: Webhook + Polling + Idempotencia

El conector usa **tres pilares** para traer las ventas de TiendaNube a Odoo sin duplicados y con la menor demora posible.

### 2.0 Cómo funciona cada paso (desde Odoo)

**Paso 1 – Webhook (entrada del aviso)**  
- TiendaNube hace una venta y envía un POST a la URL del webhook de Odoo (`/tiendanube/webhook/order`).  
- Odoo recibe el aviso y **siempre** guarda una **notificación** en `tn.webhook.notification` (ID de orden, tipo de evento, fecha, datos raw). Así queda registro de que el aviso llegó.  
- Si en la configuración de TiendaNube tienes **webhook habilitado** y hay una configuración válida (token, store, etc.), Odoo **intenta procesar la orden en ese momento**: pide a la API de TiendaNube los datos completos de esa orden (GET por ID) y llama al mismo proceso que usa el polling para **crear o actualizar** la venta en Odoo (`tn.sale.order`).  
- Si ese procesamiento inmediato **funciona**, la orden aparece en la ventana de Ventas al instante y la notificación se marca como **Procesada**.  
- Si **falla** (API caída, timeout, etc.), la notificación queda **Pendiente** y no se pierde nada: el siguiente paso (polling) se encargará de esa orden.

**Paso 2 – Polling (sincronización por fecha)**  
- El **polling** es una tarea que se ejecuta **a intervalos** (cron, p. ej. cada 10 minutos) o **manual** (“Sincronizar Órdenes Ahora (polling)”).  
- Odoo consulta a la API de TiendaNube **todas las órdenes** desde la última fecha de sincronización (o desde hace N días si es la primera vez).  
- Para **cada orden** devuelta por la API, Odoo aplica la misma lógica: **crear** si no existe en Odoo o **actualizar** si ya existe. No se crean duplicados porque se identifica la venta por **ID de orden TiendaNube + compañía**.  
- Así se cubren: órdenes que no dispararon webhook, webhooks que fallaron al procesar, y actualizaciones de estado (envío, etc.).  
- Cuando una orden se procesa correctamente (creada o actualizada), Odoo marca como **Procesada** las notificaciones de webhook pendientes para esa orden y esa compañía.

**Paso 3 – Idempotencia (sin duplicados)**  
- Tanto el webhook como el polling usan el **mismo proceso** para crear/actualizar: `create_order_from_webhook`.  
- En Odoo, una venta de TiendaNube se identifica de forma única por: **ID de la orden en TiendaNube** + **compañía**. Existe una restricción en base de datos que impide tener dos ventas con el mismo ID de TiendaNube en la misma compañía.  
- Si llega **la misma orden dos veces** (por webhook y luego por polling, o dos webhooks, o ejecutar el polling varias veces), Odoo **actualiza** la venta existente en lugar de crear otra. Eso es la **idempotencia**: repetir la operación no cambia el resultado final (una sola venta actualizada).

**Resumen**  
- **Webhook**: aviso inmediato → se guarda notificación y se intenta crear/actualizar la orden al instante.  
- **Polling**: revisión periódica por fecha → crea/actualiza órdenes que falten o no se hayan procesado por webhook.  
- **Idempotencia**: mismo ID de orden + compañía = siempre una sola venta en Odoo, creada o actualizada según corresponda.

---

## 3. Cosas que se recomienda cambiar o mejorar

### 3.1 Orden de carga de datos en el manifest
- **Recomendación:** Cargar los crons después de las vistas, para que las referencias a `model_tn_config` y `model_tn_publication` estén ya definidas. En la práctica suele funcionar igual porque los modelos se cargan antes, pero en actualizaciones puede ser más estable: poner `data/cron_*.xml` al final del bloque `data`.

### 3.2 Timeout y reintentos en API
- **Recomendación:** Revisar que los timeouts (p. ej. 30 s en `_make_request`) sean adecuados para tu red. Para operaciones masivas (importar muchas publicaciones o órdenes), valorar reintentos con backoff exponencial además del manejo actual del 429.

### 3.3 Logs en producción
- **Recomendación:** Evitar loguear JSON completos de órdenes en nivel INFO en producción (datos sensibles y volumen). Usar DEBUG para payloads completos y dejar en INFO solo resúmenes (IDs, contadores, errores).

### 3.4 Notificaciones de webhook no utilizadas
- **Estado actual:** Se guardan en `tn.webhook.notification` con estado `pending` y no se marcan como procesadas cuando el cron procesa la orden.
- **Recomendación (opcional):** Tras procesar órdenes en el cron, actualizar las notificaciones correspondientes a `processed` (por `order_id` + compañía/config) para tener trazabilidad y limpieza.

### 3.5 Vista / menú de notificaciones de webhook
- **Estado actual:** No hay menú ni vista para `tn.webhook.notification`; solo existe el modelo y permisos.
- **Recomendación (opcional):** Añadir una vista lista (y opcionalmente formulario) y un menú bajo Configuración o Ventas para consultar y depurar notificaciones.

### 3.6 Tests automáticos
- **Recomendación:** Añadir tests (p. ej. `tests/`) para: creación de configuración, creación de publicación, idempotencia de órdenes (mismo `tn_order_id` no duplica), y que el cron filtre por `polling_enabled` y por compañía. No sustituyen las pruebas manuales pero ayudan antes de desplegar.

---

## 4. Cómo comprobar que todo funcione antes de salir a producción

### 4.1 Instalación y configuración básica
- [ ] Módulo instalado sin errores (sin fallos de vista, modelo o permisos).
- [ ] Crear al menos una configuración TN con Client ID, Client Secret, Store ID.
- [ ] Ejecutar “Autorizar aplicación” y comprobar que se guardan Access Token y Refresh Token.
- [ ] “Probar conexión” responde correctamente.

### 4.2 Publicaciones y productos
- [ ] Crear una publicación desde un producto Odoo (asistente, eligiendo cuenta TN).
- [ ] Exportar la publicación a TiendaNube y comprobar que tiene `tn_product_id`.
- [ ] “Sincronizar Stock y Precio” en esa publicación: no debe dar error “variantes no tienen ID” (se rellenan desde la API si hace falta).
- [ ] Comprobar en TN que el producto existe y que stock/precio se actualizaron.

### 4.3 Órdenes (webhook + polling + idempotencia)
- [ ] Webhook: en TN configurar la URL del webhook (tu dominio + `/tiendanube/webhook/order`). Hacer una venta de prueba y comprobar en logs que llega el POST y que se crea un registro en `tn.webhook.notification` (si tienes vista o desde BD).
- [ ] Polling: con “Sincronizar Órdenes Ahora” (o esperando al cron), comprobar que las órdenes de TN se crean en Odoo como `tn.sale.order` y que no se duplican al volver a ejecutar (idempotencia).
- [ ] Crear orden de venta en Odoo desde una `tn.sale.order`: flujo completo hasta factura/almacén si usas esas opciones.

### 4.4 Multi-compañía (si aplica)
- [ ] Dos configuraciones TN, cada una con `company_id` distinto.
- [ ] Ejecutar el cron (o “Sincronizar Órdenes Ahora” por cuenta) y comprobar que las órdenes se crean en la compañía correcta (revisar `company_id` del `tn.sale.order`).

### 4.5 Cron y estado
- [ ] En la configuración TN, comprobar que “Estado del cron” muestra próxima ejecución, última ejecución y que el cron está activo.
- [ ] En Configuración > Técnico > Acciones planificadas, localizar “Sincronizar Órdenes de TiendaNube (Polling)”, verificar intervalo (p. ej. 10 min) y ejecutar “Ejecutar ahora” una vez para comprobar que no hay error.

### 4.6 Opciones avanzadas (si las usas)
- [ ] **Stock automático desde Odoo:** cambiar stock en Odoo y comprobar que se actualiza en TN (y que no hay errores 429 si hay muchas publicaciones).
- [ ] **Stock mínimo (pausar/despausar):** publicaciones con umbral configurado; bajar stock por debajo del umbral y comprobar que la publicación se pausa en TN.
- [ ] **Facturación automática:** crear orden desde TN y comprobar que se genera la factura según la opción de la configuración.

### 4.7 Seguridad y URL pública
- [ ] La URL del webhook es accesible desde internet (TiendaNube debe poder hacer POST). Si usas HTTPS y/o firewall, comprobar que no se bloquea.
- [ ] No dejar credenciales (tokens, client secret) en logs ni en código; revisar niveles de log antes de producción.

### 4.8 Resumen rápido pre-producción
1. Instalar y configurar una cuenta TN; probar conexión.
2. Exportar al menos una publicación y sincronizar stock/precio.
3. Simular venta en TN; comprobar webhook + polling y que la orden aparece en Odoo sin duplicados.
4. Revisar estado del cron y ejecución manual.
5. Si hay varias compañías, comprobar que las órdenes van a la compañía de cada configuración.

Si todo lo anterior se cumple, el conector está en condiciones de usarse en producción con supervisión inicial (revisar logs las primeras 24–48 h).

---

## 5. Cómo solucionar el resto (sin programar aquí)

### Marcar notificaciones de webhook como procesadas cuando el cron procese la orden
- **Qué hacer:** Después de crear o actualizar una `tn.sale.order` en `sync_orders_by_polling` (o en `create_order_from_webhook` cuando se llame desde el cron), buscar en `tn.webhook.notification` los registros con ese `order_id` y misma compañía/config, y hacer `write({'status': 'processed'})`.
- **Dónde:** Por ejemplo al final del bucle en `sync_orders_by_polling` por cada `order_id` procesado, o en un método que se llame tras procesar cada orden. Así se distingue qué avisos del webhook ya fueron cubiertos por el polling.

### Vista y menú para `tn.webhook.notification`
- **Qué hacer:** Crear un archivo XML de vistas (lista y opcionalmente formulario) para el modelo `tn.webhook.notification`, y un `ir.actions.act_window` con ese modelo. Luego añadir un ítem de menú (por ejemplo bajo Configuración > TiendaNube o Ventas) que ejecute esa acción. Así se puede consultar y filtrar notificaciones por estado, fecha, order_id, etc.

### Tests automáticos (idempotencia, compañía, cron)
- **Qué hacer:** Añadir carpeta `tests/` en el módulo, con `__init__.py` y módulos que hereden de `TransactionCase` (o `HttpCase` si hace falta request). Tests sugeridos: (1) crear dos veces la misma orden con el mismo `tn_order_id` y misma compañía y comprobar que solo existe un registro; (2) con dos compañías y dos configs, ejecutar polling y comprobar que las órdenes tienen `company_id` de la config correspondiente; (3) llamar a `cron_sync_orders_by_polling` y comprobar que solo se procesan configs con `polling_enabled=True`. Registrar los tests en `__init__.py` del módulo y ejecutar con `odoo-bin -i tiendanube_connector --test-enable --stop-after-init`.

### Orden de carga de datos en el manifest
- **Qué hacer:** En `__manifest__.py`, en la lista `data`, colocar los XML de crons al final, por ejemplo después de todas las vistas: `'data/cron_order_sync.xml'` y `'data/cron_product_import.xml'` al final. Así se cargan primero modelos y vistas y después las acciones planificadas que referencian `model_tn_config` y `model_tn_publication`, reduciendo riesgo de errores en actualizaciones.

### Timeouts y reintentos en llamadas a la API
- **Qué hacer:** En `_make_request` (en `tn_sync.py`), el timeout está fijo (p. ej. 30 s). Para producción se puede: (1) hacer el timeout configurable en `tn.config` (campo numérico en segundos) y usarlo en la petición; (2) en caso de timeout o error de conexión, aplicar reintentos con backoff exponencial (p. ej. 2^intento segundos) además del manejo actual del 429; (3) no superar un tiempo máximo total (p. ej. 2 minutos) entre todos los reintentos. Los errores 429 ya se manejan con `Retry-After`; los timeouts y 5xx se benefician de reintentos limitados.

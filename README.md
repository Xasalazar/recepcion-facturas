# Recepción automatizada de facturas de proveedores

Flujo automatizado que recibe facturas en PDF, extrae sus datos con un **modelo de IA**,
las **valida contra el ERP** y, según el resultado, **registra** la factura o **envía un
correo de aviso** explicando por qué no se pudo registrar.

## Flujo (por cada factura)

1. **Lectura del PDF** → se extrae el texto.
2. **Extracción con IA** (Claude Sonnet / Anthropic) → JSON con: RUC emisor, número de
   factura, fecha de emisión, orden de compra, subtotal, IVA y total.
   - Si el modelo falla (sobrecarga, JSON inválido, datos incompletos) se usa **extracción
     por texto** del PDF como respaldo.
3. **Validaciones:**
   - Formato del RUC (13 dígitos) y del número (`ddd-ddd-ddddddddd`).
   - El **proveedor existe** y está **ACTIVO** (`GET /proveedores/{ruc}`).
   - La **orden de compra existe** (`GET /ordenes-compra/{numero}`) y **pertenece al RUC**.
4. **Si todo es correcto** → registra la factura (`POST /facturas`).
   - Si ya estaba registrada (HTTP 409) se trata de forma **idempotente** (no duplica).
5. **Si hay un error de negocio** (proveedor inexistente/inactivo, OC ajena, formato) →
   **no registra** y **envía un correo** a Compras.
   - Un **error técnico** (ERP caído, timeout, HTTP 5xx) **no registra** y **no avisa** a
     Compras: no es un problema de la factura.

## Requisitos

- Python 3.10+
- Dependencias: `pip install -r requirements.txt`

## Configuración (variables de entorno)

Copia `.env.example` a `.env` y completa los valores. **Los secretos (token, API key y
contraseñas) van en el `.env`, nunca en el código.**

| Variable | Descripción |
|---|---|
| `ERP_BASE_URL` | URL del ERP, ej. `http://192.168.15.113:8000` |
| `ERP_TOKEN` | Token personal (se envía como `Authorization: Bearer`) |
| `ANTHROPIC_API_KEY` | API key de Claude |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` |
| `SMTP_HOST`/`SMTP_PORT` | Servidor de correo (desarrollo: buzón de prueba `192.168.15.113:1025`) |
| `SMTP_USER`/`SMTP_PASS` | Credenciales SMTP (vacío en el buzón de prueba) |
| `SMTP_TLS` | `true`/`false` (STARTTLS) |
| `MAIL_FROM`/`MAIL_TO` | Remitente y destinatario de los avisos |

## Uso

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # y completar
```

### Bandeja web (presentación y pruebas extra)

Abre una bandeja para **soltar PDF** y ver el resultado (registrada / rechazada / error) sin tocar código. Sirve para la corrida de F1–F3 y para las facturas adicionales que entregue el evaluador.

```bash
python web.py
```

Luego abre [http://127.0.0.1:5055](http://127.0.0.1:5055).

- **Procesar F1, F2 y F3** usa los PDF de `facturas/`.
- **Suelte los PDF aquí** acepta uno o varios archivos nuevos (pruebas adicionales).
- La **bitácora** de la página muestra cada paso (IA, ERP, correo). Lo mismo queda en
  `salidas/bitacora.log` y un JSON por factura.

### Script (terminal)

```bash
python procesar.py facturas/F1.pdf facturas/F2.pdf facturas/F3.pdf
```

Cada corrida de terminal imprime el detalle y guarda un resumen en `salidas/`. La bandeja web usa el mismo flujo (`procesar_factura`).

## Cómo cambiar el correo a producción

En el `.env`, apuntar a tu SMTP real (ej. ToqueLab o Gmail con contraseña de aplicación):

```
SMTP_HOST=smtp.tu-servidor.com
SMTP_PORT=587
SMTP_USER=usuario
SMTP_PASS=contraseña-de-aplicacion
SMTP_TLS=true
MAIL_FROM=remitente@dominio.com
MAIL_TO=destinatario@dominio.com
```

## Notas de diseño (para el área TIC)

- **Secretos fuera del código** (variables de entorno / `.env` en `.gitignore`).
- **Configuración al arranque**: si faltan `ERP_BASE_URL`, `ERP_TOKEN`,
  `ANTHROPIC_API_KEY` o `SMTP_HOST`, el script sale con un mensaje claro.
- **Idempotencia** en el registro (HTTP 409 = ya registrada, no se duplica).
- **Reintentos con backoff** ante sobrecarga temporal del modelo de IA; si el modelo no
  responde, extracción por texto del PDF.
- **Error técnico ≠ rechazo de negocio**: timeout/5xx del ERP no genera correo a Compras.
- **Trazabilidad**: cada corrida deja un resumen JSON con lo registrado/rechazado/error.
- **Separación de responsabilidades**: lectura, extracción IA, extracción por texto,
  validación, registro y notificación son funciones independientes.

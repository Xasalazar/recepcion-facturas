# Recepción automatizada de facturas de proveedores

Flujo automatizado que recibe facturas en PDF, extrae sus datos con un **modelo de IA**,
las **valida contra el ERP** y, según el resultado, **registra** la factura o **envía un
correo de aviso** explicando por qué no se pudo registrar.

## Flujo (por cada factura)

1. **Lectura del PDF** → se extrae el texto.
2. **Extracción con IA** (Claude Sonnet / Anthropic) → JSON con: RUC emisor, número de
   factura, fecha de emisión, orden de compra, subtotal, IVA y total.
3. **Validaciones:**
   - Formato del RUC (13 dígitos) y del número (`ddd-ddd-ddddddddd`).
   - El **proveedor existe** y está **ACTIVO** (`GET /proveedores/{ruc}`).
   - La **orden de compra existe** (`GET /ordenes-compra/{numero}`) y **pertenece al RUC**.
4. **Si todo es correcto** → registra la factura (`POST /facturas`).
   - Si ya estaba registrada (HTTP 409) se trata de forma **idempotente** (no duplica).
5. **Si hay algún error** → **no registra** y **envía un correo** con número de factura,
   RUC y el motivo explicado para el área de Compras.

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

python procesar.py facturas/F1.pdf facturas/F2.pdf facturas/F3.pdf
```

Cada corrida imprime el detalle por factura y guarda un resumen en `salidas/`.

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
- **Idempotencia** en el registro (HTTP 409 = ya registrada, no se duplica).
- **Reintentos con backoff** ante sobrecarga temporal del modelo de IA.
- **Trazabilidad**: cada corrida deja un resumen JSON con lo registrado/rechazado.
- **Separación de responsabilidades**: lectura, extracción IA, validación, registro y
  notificación son funciones independientes y fáciles de testear o reemplazar.

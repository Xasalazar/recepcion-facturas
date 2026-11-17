#!/usr/bin/env python3
"""
Recepción automatizada de facturas de proveedores.

Flujo por cada PDF:
  1. Extrae el texto del PDF.
  2. Extrae los datos con IA (Gemini) -> JSON estructurado.
  3. Valida formato (RUC, número de factura).
  4. Consulta el ERP: proveedor existe y ACTIVO; OC existe y pertenece al RUC.
  5. Si todo OK  -> registra la factura (POST /facturas).
     Si hay error -> NO registra y envía un correo explicando el motivo.

Uso:
  python procesar.py facturas/F1.pdf facturas/F2.pdf facturas/F3.pdf
  python procesar.py facturas/*.pdf

Los secretos (token, API key, contraseñas) se leen de variables de entorno (.env),
NUNCA van escritos en el código.
"""
import os
import re
import sys
import json
import smtplib
from email.message import EmailMessage
from datetime import datetime

import requests
from pypdf import PdfReader
from dotenv import load_dotenv
import anthropic

load_dotenv()

ERP_BASE_URL = os.getenv("ERP_BASE_URL", "").rstrip("/")
ERP_TOKEN = os.getenv("ERP_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "1025"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_TLS = os.getenv("SMTP_TLS", "false").lower() == "true"
MAIL_FROM = os.getenv("MAIL_FROM", "avisos@prueba.local")
MAIL_TO = os.getenv("MAIL_TO", "avisos@prueba.local")

RE_RUC = re.compile(r"^\d{13}$")
RE_NUMERO = re.compile(r"^\d{3}-\d{3}-\d{9}$")

HEADERS = {"Authorization": f"Bearer {ERP_TOKEN}"}


# ---------- 1. Leer PDF ----------
def leer_pdf(ruta: str) -> str:
    reader = PdfReader(ruta)
    return "\n".join((p.extract_text() or "") for p in reader.pages)


# ---------- 2. Extraer datos con IA ----------
def extraer_con_ia(texto: str, reintentos: int = 5) -> dict:
    import time
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = (
        "Eres un extractor de datos de facturas electronicas ecuatorianas. "
        "Del siguiente texto de una factura, devuelve SOLO un JSON valido (sin markdown, "
        "sin explicaciones) con estas claves exactas:\n"
        '  "ruc": RUC del EMISOR (13 digitos, string),\n'
        '  "numero_factura": formato ddd-ddd-ddddddddd (string),\n'
        '  "fecha_emision": formato YYYY-MM-DD (string),\n'
        '  "orden_compra": numero de la orden de compra (string),\n'
        '  "subtotal": numero,\n'
        '  "iva": numero,\n'
        '  "total": numero.\n'
        "El RUC es el del proveedor/emisor, NO el del comprador.\n\nTEXTO:\n" + texto
    )
    ultimo_error = None
    for intento in range(1, reintentos + 1):
        try:
            resp = client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            crudo = resp.content[0].text.strip()
            # por si el modelo envuelve en ```json ... ```
            if crudo.startswith("```"):
                crudo = crudo.strip("`")
                crudo = crudo[crudo.index("{"): crudo.rindex("}") + 1]
            return json.loads(crudo)
        except Exception as e:
            ultimo_error = e
            msg = str(e)
            if any(c in msg for c in ("overloaded", "529", "503", "429", "rate")):
                espera = min(2 ** intento, 20)
                print(f"     (modelo ocupado, reintento {intento}/{reintentos} en {espera}s...)")
                time.sleep(espera)
                continue
            raise
    raise RuntimeError(f"IA no disponible tras {reintentos} intentos: {ultimo_error}")


# ---------- 3-4. Validaciones ----------
def validar(datos: dict) -> list:
    """Devuelve lista de motivos de error (vacía = todo OK)."""
    errores = []

    ruc = str(datos.get("ruc", "")).strip()
    numero = str(datos.get("numero_factura", "")).strip()
    oc = str(datos.get("orden_compra", "")).strip()

    if not RE_RUC.match(ruc):
        errores.append(f"El RUC '{ruc}' no tiene el formato válido de 13 dígitos.")
    if not RE_NUMERO.match(numero):
        errores.append(f"El número de factura '{numero}' no tiene el formato ddd-ddd-ddddddddd.")

    # Proveedor
    r = requests.get(f"{ERP_BASE_URL}/proveedores/{ruc}", headers=HEADERS, timeout=15)
    if r.status_code == 404:
        errores.append(f"El proveedor con RUC {ruc} no está registrado en el sistema.")
    elif r.ok:
        prov = r.json()
        if prov.get("estado", "").upper() != "ACTIVO":
            errores.append(f"El proveedor {prov.get('razon_social', ruc)} no está activo (estado: {prov.get('estado')}).")
    else:
        errores.append(f"No se pudo consultar el proveedor (HTTP {r.status_code}).")

    # Orden de compra
    r = requests.get(f"{ERP_BASE_URL}/ordenes-compra/{oc}", headers=HEADERS, timeout=15)
    if r.status_code == 404:
        errores.append(f"La orden de compra {oc} no existe en el sistema.")
    elif r.ok:
        orden = r.json()
        if orden.get("ruc_proveedor") and orden["ruc_proveedor"] != ruc:
            errores.append(
                f"La orden de compra {oc} pertenece a otro proveedor "
                f"(RUC {orden['ruc_proveedor']}), no al emisor de la factura."
            )
    else:
        errores.append(f"No se pudo consultar la orden de compra (HTTP {r.status_code}).")

    return errores


# ---------- 5a. Registrar ----------
def registrar(datos: dict) -> dict:
    payload = {
        "ruc": str(datos["ruc"]).strip(),
        "numero_factura": str(datos["numero_factura"]).strip(),
        "fecha_emision": str(datos["fecha_emision"]).strip(),
        "orden_compra": str(datos["orden_compra"]).strip(),
        "subtotal": float(datos["subtotal"]),
        "iva": float(datos["iva"]),
        "total": float(datos["total"]),
    }
    r = requests.post(f"{ERP_BASE_URL}/facturas", headers=HEADERS, json=payload, timeout=15)
    if r.status_code == 409:
        # Ya estaba registrada: idempotencia, no es un error de negocio.
        return {"id": "ya_registrada", "duplicada": True}
    r.raise_for_status()
    return r.json()


# ---------- 5b. Enviar correo de aviso ----------
def enviar_aviso(numero: str, ruc: str, motivos: list):
    cuerpo = (
        f"No se pudo registrar la siguiente factura:\n\n"
        f"  Número de factura: {numero}\n"
        f"  RUC del emisor: {ruc}\n\n"
        f"Motivo(s):\n" + "\n".join(f"  - {m}" for m in motivos) + "\n\n"
        f"Por favor revisar con el área de Compras antes de reintentar.\n"
    )
    msg = EmailMessage()
    msg["Subject"] = f"Factura {numero} NO registrada"
    msg["From"] = MAIL_FROM
    msg["To"] = MAIL_TO
    msg.set_content(cuerpo)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
        if SMTP_TLS:
            s.starttls()
        if SMTP_USER:
            s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)


# ---------- Orquestación ----------
def procesar_factura(ruta: str) -> dict:
    nombre = os.path.basename(ruta)
    print(f"\n=== Procesando {nombre} ===")
    texto = leer_pdf(ruta)
    datos = extraer_con_ia(texto)
    print("  IA extrajo:", json.dumps(datos, ensure_ascii=False))

    numero = str(datos.get("numero_factura", "?"))
    ruc = str(datos.get("ruc", "?"))

    errores = validar(datos)
    if errores:
        print("  ❌ RECHAZADA:")
        for e in errores:
            print("     -", e)
        enviar_aviso(numero, ruc, errores)
        print(f"  ✉️  Aviso enviado a {MAIL_TO}")
        return {"factura": nombre, "numero": numero, "estado": "rechazada", "motivos": errores}

    res = registrar(datos)
    if res.get("duplicada"):
        print("  ✅ YA REGISTRADA (idempotencia: no se duplica)")
        return {"factura": nombre, "numero": numero, "estado": "ya_registrada"}
    print(f"  ✅ REGISTRADA (id {res.get('id')})")
    return {"factura": nombre, "numero": numero, "estado": "registrada", "id": res.get("id")}


def main():
    rutas = sys.argv[1:]
    if not rutas:
        print("Uso: python procesar.py <factura1.pdf> [factura2.pdf ...]")
        sys.exit(1)

    resumen = []
    for ruta in rutas:
        try:
            resumen.append(procesar_factura(ruta))
        except Exception as e:
            print(f"  ⚠️  Error procesando {ruta}: {e}")
            resumen.append({"factura": os.path.basename(ruta), "estado": "error", "detalle": str(e)})

    print("\n==================== RESUMEN ====================")
    for r in resumen:
        print(f"  {r['factura']:10} -> {r['estado']}")
    # Guardar resumen
    os.makedirs("salidas", exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    with open(f"salidas/resumen-{stamp}.json", "w", encoding="utf-8") as f:
        json.dump(resumen, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()

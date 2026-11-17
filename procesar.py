#!/usr/bin/env python3
"""
Recepción automatizada de facturas de proveedores.

Flujo por cada PDF:
  1. Extrae el texto del PDF.
  2. Extrae los datos con IA (Claude / Anthropic) -> JSON estructurado.
     Si el modelo falla, extrae por texto (regex) como respaldo.
  3. Valida formato (RUC, número de factura).
  4. Consulta el ERP: proveedor existe y ACTIVO; OC existe y pertenece al RUC.
  5. Si todo OK           -> registra la factura (POST /facturas).
     Si hay error de negocio -> NO registra y envía un correo explicando el motivo.
     Si hay error técnico    -> NO registra y NO avisa a Compras (se reintenta luego).

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
import time
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
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_TLS = os.getenv("SMTP_TLS", "false").lower() == "true"
MAIL_FROM = os.getenv("MAIL_FROM", "avisos@prueba.local")
MAIL_TO = os.getenv("MAIL_TO", "avisos@prueba.local")

RE_RUC = re.compile(r"^\d{13}$")
RE_NUMERO = re.compile(r"^\d{3}-\d{3}-\d{9}$")
CAMPOS_FACTURA = (
    "ruc",
    "numero_factura",
    "fecha_emision",
    "orden_compra",
    "subtotal",
    "iva",
    "total",
)
HEADERS = {"Authorization": f"Bearer {ERP_TOKEN}"}
MUESTRAS = ("F1.pdf", "F2.pdf", "F3.pdf")
CARPETA_SALIDAS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "salidas")
ARCHIVO_BITACORA = os.path.join(CARPETA_SALIDAS, "bitacora.log")
ARCHIVO_JSONL = os.path.join(CARPETA_SALIDAS, "bitacora.jsonl")
_cliente_ia = None


class ErrorTecnico(RuntimeError):
    """Fallo de infraestructura. No se registra y no se avisa a Compras."""


def _entero_env(nombre: str, default: str) -> int:
    crudo = os.getenv(nombre, default)
    try:
        return int(crudo)
    except (TypeError, ValueError):
        print(f"Error: {nombre} debe ser un entero, recibido {crudo!r}.")
        sys.exit(1)


SMTP_PORT = _entero_env("SMTP_PORT", "1025")


def faltantes_config() -> list:
    return [
        nombre
        for nombre, valor in (
            ("ERP_BASE_URL", ERP_BASE_URL),
            ("ERP_TOKEN", ERP_TOKEN),
            ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY),
            ("SMTP_HOST", SMTP_HOST),
        )
        if not valor
    ]


def validar_config() -> None:
    faltantes = faltantes_config()
    if faltantes:
        print("Faltan variables de entorno: " + ", ".join(faltantes))
        print("Copia .env.example a .env y completa los valores.")
        sys.exit(1)


def log_paso(pasos: list, mensaje: str) -> None:
    marca = datetime.now().strftime("%H:%M:%S")
    linea = f"{marca}  {mensaje}"
    print(linea, flush=True)
    os.makedirs(CARPETA_SALIDAS, exist_ok=True)
    with open(ARCHIVO_BITACORA, "a", encoding="utf-8") as f:
        f.write(datetime.now().strftime("%Y-%m-%d ") + linea + "\n")
    pasos.append(linea)


def guardar_resultado(resultado: dict) -> str:
    os.makedirs(CARPETA_SALIDAS, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = os.path.splitext(os.path.basename(str(resultado.get("factura", "factura"))))[0]
    ruta = os.path.join(CARPETA_SALIDAS, f"{stamp}-{base}.json")
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(resultado, f, ensure_ascii=False, indent=2)
    with open(ARCHIVO_JSONL, "a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {"cuando": datetime.now().isoformat(timespec="seconds"), **resultado},
                ensure_ascii=False,
            )
            + "\n"
        )
    return ruta


def leer_historial(limite: int = 40) -> list:
    if not os.path.isfile(ARCHIVO_JSONL):
        return []
    with open(ARCHIVO_JSONL, encoding="utf-8") as f:
        lineas = [ln for ln in f.read().splitlines() if ln.strip()]
    items = []
    for ln in lineas[-limite:]:
        try:
            items.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    items.reverse()
    return items


# ---------- 1. Leer PDF ----------
def leer_pdf(ruta: str) -> str:
    reader = PdfReader(ruta)
    return "\n".join((p.extract_text() or "") for p in reader.pages)


# ---------- 2. Extraer datos ----------
def _cliente() -> anthropic.Anthropic:
    global _cliente_ia
    if _cliente_ia is None:
        _cliente_ia = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _cliente_ia


def _json_desde_texto(crudo: str) -> dict:
    crudo = crudo.strip()
    if crudo.startswith("```"):
        crudo = crudo.strip("`")
        inicio = crudo.find("{")
        fin = crudo.rfind("}")
        if inicio == -1 or fin == -1:
            raise ValueError("La IA no devolvió un JSON.")
        crudo = crudo[inicio : fin + 1]
    datos = json.loads(crudo)
    if not isinstance(datos, dict):
        raise ValueError("La IA no devolvió un objeto JSON.")
    return datos


def datos_completos(datos: dict) -> bool:
    return all(datos.get(campo) not in (None, "") for campo in CAMPOS_FACTURA)


def extraer_con_ia(texto: str, reintentos: int = 5) -> dict:
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
            resp = _cliente().messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            return _json_desde_texto(resp.content[0].text)
        except Exception as e:
            ultimo_error = e
            msg = str(e).lower()
            if any(c in msg for c in ("overloaded", "529", "503", "429", "rate")):
                espera = min(2 ** intento, 20)
                print(
                    f"     (modelo ocupado, reintento {intento}/{reintentos} en {espera}s...)",
                    flush=True,
                )
                time.sleep(espera)
                continue
            raise
    raise RuntimeError(f"IA no disponible tras {reintentos} intentos: {ultimo_error}")


def _monto(crudo: str) -> float:
    return float(crudo.replace(",", ""))


def extraer_por_texto(texto: str) -> dict:
    """Extracción determinista a partir del texto del PDF (respaldo si falla la IA)."""
    ruc = re.search(r"(?im)^RUC:\s*(\d{13})\s*$", texto)
    numero = re.search(r"(?i)No\.\s*(\d{3}-\d{3}-\d{9})", texto)
    fecha = re.search(r"(?i)Fecha de emisión:\s*(\d{1,2})/(\d{1,2})/(\d{4})", texto)
    oc = re.search(r"(?i)Orden de compra:\s*(\S+)", texto)
    subtotal = re.search(r"(?i)SUBTOTAL\s+15\s*%\s*\$\s*([\d.,]+)", texto)
    iva = re.search(r"(?i)IVA\s+15\s*%\s*\$\s*([\d.,]+)", texto)
    total = re.search(r"(?i)VALOR TOTAL\s*\$\s*([\d.,]+)", texto)

    faltantes = [
        nombre
        for nombre, hallado in (
            ("ruc", ruc),
            ("numero_factura", numero),
            ("fecha_emision", fecha),
            ("orden_compra", oc),
            ("subtotal", subtotal),
            ("iva", iva),
            ("total", total),
        )
        if not hallado
    ]
    if faltantes:
        raise ValueError(
            "No se pudieron extraer del texto los campos: " + ", ".join(faltantes)
        )

    return {
        "ruc": ruc.group(1),
        "numero_factura": numero.group(1),
        "fecha_emision": f"{fecha.group(3)}-{fecha.group(2).zfill(2)}-{fecha.group(1).zfill(2)}",
        "orden_compra": oc.group(1),
        "subtotal": _monto(subtotal.group(1)),
        "iva": _monto(iva.group(1)),
        "total": _monto(total.group(1)),
    }


def extraer_datos(texto: str, log=None) -> tuple:
    """Intenta IA; si el modelo falla o el JSON sale incompleto, usa extracción por texto."""
    def _log(mensaje: str) -> None:
        if log:
            log(mensaje)
        else:
            print(mensaje, flush=True)

    try:
        datos = extraer_con_ia(texto)
        if datos_completos(datos):
            _log("Extracción con IA correcta.")
            return datos, "ia"
        _log("IA devolvió datos incompletos; se usa extracción por texto.")
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as e:
        _log(f"IA no disponible ({e}); se usa extracción por texto.")
    return extraer_por_texto(texto), "texto"


# ---------- 3-4. Validaciones ----------
def consultar_erp(recurso: str, identificador: str) -> requests.Response:
    url = f"{ERP_BASE_URL}/{recurso}/{identificador}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
    except requests.RequestException as e:
        raise ErrorTecnico(f"No se pudo consultar {recurso} (red/timeout): {e}") from e
    if r.status_code == 404:
        return r
    if not r.ok:
        raise ErrorTecnico(f"No se pudo consultar {recurso} (HTTP {r.status_code}).")
    return r


def validar(datos: dict, log=None) -> list:
    """Devuelve lista de motivos de negocio (vacía = todo OK). Errores técnicos se lanzan."""
    def _log(mensaje: str) -> None:
        if log:
            log(mensaje)

    errores = []

    ruc = str(datos.get("ruc", "")).strip()
    numero = str(datos.get("numero_factura", "")).strip()
    oc = str(datos.get("orden_compra", "")).strip()

    if not RE_RUC.match(ruc):
        errores.append(f"El RUC '{ruc}' no tiene el formato válido de 13 dígitos.")
    if not RE_NUMERO.match(numero):
        errores.append(f"El número de factura '{numero}' no tiene el formato ddd-ddd-ddddddddd.")

    if RE_RUC.match(ruc):
        _log(f"Consultando proveedor {ruc} en el ERP.")
        r = consultar_erp("proveedores", ruc)
        _log(f"ERP GET /proveedores/{ruc} -> HTTP {r.status_code}.")
        if r.status_code == 404:
            errores.append(f"El proveedor con RUC {ruc} no está registrado en el sistema.")
        else:
            prov = r.json()
            if prov.get("estado", "").upper() != "ACTIVO":
                errores.append(
                    f"El proveedor {prov.get('razon_social', ruc)} no está activo "
                    f"(estado: {prov.get('estado')})."
                )
            else:
                _log(f"Proveedor activo: {prov.get('razon_social', ruc)}.")

    if not oc:
        errores.append("La factura no indica orden de compra.")
    else:
        _log(f"Consultando orden de compra {oc} en el ERP.")
        r = consultar_erp("ordenes-compra", oc)
        _log(f"ERP GET /ordenes-compra/{oc} -> HTTP {r.status_code}.")
        if r.status_code == 404:
            errores.append(f"La orden de compra {oc} no existe en el sistema.")
        else:
            orden = r.json()
            if orden.get("ruc_proveedor") and orden["ruc_proveedor"] != ruc:
                errores.append(
                    f"La orden de compra {oc} pertenece a otro proveedor "
                    f"(RUC {orden['ruc_proveedor']}), no al emisor de la factura."
                )
            else:
                _log(f"La orden de compra {oc} coincide con el emisor.")

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
    try:
        r = requests.post(f"{ERP_BASE_URL}/facturas", headers=HEADERS, json=payload, timeout=15)
    except requests.RequestException as e:
        raise ErrorTecnico(f"No se pudo registrar en el ERP (red/timeout): {e}") from e
    if r.status_code == 409:
        # Ya estaba registrada: idempotencia, no es un error de negocio.
        return {"id": "ya_registrada", "duplicada": True, "http": 409}
    if not r.ok:
        raise ErrorTecnico(f"No se pudo registrar en el ERP (HTTP {r.status_code}).")
    cuerpo = r.json()
    cuerpo["http"] = r.status_code
    return cuerpo


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

    if SMTP_PORT == 465:
        cliente = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20)
    else:
        cliente = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20)
        if SMTP_TLS:
            cliente.starttls()
    with cliente as s:
        if SMTP_USER:
            s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)


# ---------- Orquestación ----------
def procesar_factura(ruta: str) -> dict:
    pasos = []

    def log(mensaje: str) -> None:
        log_paso(pasos, mensaje)

    def cerrar(resultado: dict) -> dict:
        resultado["pasos"] = pasos
        resultado["archivo_salida"] = os.path.relpath(guardar_resultado(resultado))
        return resultado

    nombre = os.path.basename(ruta)
    log(f"Procesando {nombre}")
    texto = leer_pdf(ruta)
    log(f"PDF leído ({len(texto)} caracteres).")
    if not texto.strip():
        raise ErrorTecnico("El PDF no tiene texto extraíble.")

    datos, origen = extraer_datos(texto, log=log)
    log("Datos extraídos: " + json.dumps(datos, ensure_ascii=False))

    numero = str(datos.get("numero_factura", "?"))
    ruc = str(datos.get("ruc", "?"))
    extra = {"datos": datos, "origen": origen, "ruc": ruc}

    try:
        errores = validar(datos, log=log)
    except ErrorTecnico as e:
        log(f"Error técnico (no se registra, no se avisa a Compras): {e}")
        return cerrar({"factura": nombre, "numero": numero, "estado": "error", "detalle": str(e), **extra})

    if errores:
        for e in errores:
            log(f"Rechazo: {e}")
        enviar_aviso(numero, ruc, errores)
        log(f"Aviso enviado a {MAIL_TO}.")
        return cerrar({
            "factura": nombre,
            "numero": numero,
            "estado": "rechazada",
            "motivos": errores,
            "aviso": True,
            **extra,
        })

    log("Validación de negocio correcta. Registrando en el ERP.")
    try:
        res = registrar(datos)
    except ErrorTecnico as e:
        log(f"Error técnico (no se registra, no se avisa a Compras): {e}")
        return cerrar({"factura": nombre, "numero": numero, "estado": "error", "detalle": str(e), **extra})

    if res.get("duplicada"):
        log("ERP HTTP 409: ya estaba registrada (idempotencia, no se duplica).")
        return cerrar({"factura": nombre, "numero": numero, "estado": "ya_registrada", **extra})
    log(f"ERP POST /facturas -> HTTP {res.get('http', 201)}. Registrada id {res.get('id')}.")
    return cerrar({"factura": nombre, "numero": numero, "estado": "registrada", "id": res.get("id"), **extra})


def main():
    rutas = sys.argv[1:]
    if not rutas:
        print("Uso: python procesar.py <factura1.pdf> [factura2.pdf ...]")
        sys.exit(1)

    validar_config()

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
    os.makedirs("salidas", exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    with open(f"salidas/resumen-{stamp}.json", "w", encoding="utf-8") as f:
        json.dump(resumen, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()

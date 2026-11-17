import json
ERP = "http://192.168.15.113:8000"
BEARER = {"id": "erpBearerXs00001", "name": "ERP Diosmar (Bearer)"}
CLAUDE = {"id": "anthropicXs00001", "name": "Anthropic API key"}
SMTP = {"id": "smtpPruebaXs0001", "name": "SMTP avisos"}

prep_code = r'''
// Config no secreta (los secretos viven en Credenciales de n8n)
const MODELO = 'claude-sonnet-4-6';
return $input.all().map(item => {
  const texto = item.json.text || '';
  const prompt = 'Eres un extractor de datos de facturas electronicas ecuatorianas. ' +
    'Del siguiente texto de una factura, devuelve SOLO un JSON valido (sin markdown, sin explicaciones) con estas claves exactas:\n' +
    '  "ruc": RUC del EMISOR (13 digitos, string),\n' +
    '  "numero_factura": formato ddd-ddd-ddddddddd (string),\n' +
    '  "fecha_emision": formato YYYY-MM-DD (string),\n' +
    '  "orden_compra": numero de la orden de compra (string),\n' +
    '  "subtotal": numero,\n  "iva": numero,\n  "total": numero.\n' +
    'El RUC es el del proveedor/emisor, NO el del comprador.\n\nTEXTO:\n' + texto;
  return { json: { claude: { model: MODELO, max_tokens: 1024, messages: [{ role: 'user', content: prompt }] } } };
});
'''

parse_code = r'''
let crudo = ($json.content && $json.content[0] && $json.content[0].text) || '';
crudo = crudo.trim();
const i = crudo.indexOf('{'), f = crudo.lastIndexOf('}');
if (i === -1 || f === -1) throw new Error('La IA no devolvio un JSON');
const d = JSON.parse(crudo.slice(i, f + 1));
const datos = {
  ruc: String(d.ruc ?? '').trim(),
  numero_factura: String(d.numero_factura ?? '').trim(),
  fecha_emision: String(d.fecha_emision ?? '').trim(),
  orden_compra: String(d.orden_compra ?? '').trim(),
  subtotal: Number(d.subtotal),
  iva: Number(d.iva),
  total: Number(d.total),
};
return { json: datos };
'''

valid_code = r'''
const datos = $('Parsear respuesta IA').item.json;
const prov = $('ERP: consultar proveedor').item.json;
const oc = $('ERP: consultar OC').item.json;
const archivo = $('Normalizar archivo').item.json.archivo;
const errores = [];

if (!/^\d{13}$/.test(datos.ruc)) errores.push(`El RUC '${datos.ruc}' no tiene el formato valido de 13 digitos.`);
if (!/^\d{3}-\d{3}-\d{9}$/.test(datos.numero_factura)) errores.push(`El numero de factura '${datos.numero_factura}' no tiene el formato ddd-ddd-ddddddddd.`);

if (prov.statusCode === 404) errores.push(`El proveedor con RUC ${datos.ruc} no esta registrado en el sistema.`);
else if (prov.statusCode >= 400) throw new Error(`Error tecnico consultando proveedor (HTTP ${prov.statusCode})`);
else if (String(prov.body.estado || '').toUpperCase() !== 'ACTIVO')
  errores.push(`El proveedor ${prov.body.razon_social || datos.ruc} no esta activo (estado: ${prov.body.estado}).`);

if (!datos.orden_compra) errores.push('La factura no indica orden de compra.');
else if (oc.statusCode === 404) errores.push(`La orden de compra ${datos.orden_compra} no existe en el sistema.`);
else if (oc.statusCode >= 400) throw new Error(`Error tecnico consultando OC (HTTP ${oc.statusCode})`);
else if (oc.body.ruc_proveedor && oc.body.ruc_proveedor !== datos.ruc)
  errores.push(`La orden de compra ${datos.orden_compra} pertenece a otro proveedor (RUC ${oc.body.ruc_proveedor}), no al emisor de la factura.`);

return { json: { archivo, ...datos, valida: errores.length === 0, errores, motivos_texto: errores.map(e => '  - ' + e).join('\n') } };
'''

reg_code = r'''
const r = $json;
const d = $('Validar reglas de negocio').item.json;
const base = { fecha: $now.toISO(), archivo: d.archivo, numero_factura: d.numero_factura, ruc: d.ruc, orden_compra: d.orden_compra, total: d.total, http_erp: r.statusCode };
if (r.statusCode === 409) return { json: { ...base, estado: 'ya_registrada', mensaje: `Factura ${d.numero_factura} ya estaba registrada (idempotencia, no se duplica).` } };
if (r.statusCode >= 400) throw new Error(`Error tecnico registrando en ERP (HTTP ${r.statusCode}): ${JSON.stringify(r.body)}`);
return { json: { ...base, estado: 'registrada', id: r.body.id, mensaje: `Factura ${d.numero_factura} REGISTRADA (id ${r.body.id}).` } };
'''

rech_code = r'''
const d = $('Validar reglas de negocio').item.json;
return { json: { fecha: $now.toISO(), archivo: d.archivo, numero_factura: d.numero_factura, ruc: d.ruc, orden_compra: d.orden_compra, total: d.total, estado: 'rechazada', motivos: d.errores, aviso_enviado: true, mensaje: `Factura ${d.numero_factura} RECHAZADA. Aviso enviado a Compras.` } };
'''

norm_code = r'''
// Un item por cada PDF subido (el formulario acepta varios)
const salida = [];
for (const item of $input.all()) {
  for (const clave of Object.keys(item.binary || {})) {
    const b = item.binary[clave];
    salida.push({ json: { archivo: b.fileName, recibido: $now.toISO() }, binary: { data: b } });
  }
}
if (!salida.length) throw new Error('No se recibio ningun PDF');
return salida;
'''

err_code = r'''
// Error tecnico en una factura: se registra en el log y las demas siguen.
let archivo = null;
try { archivo = $('Normalizar archivo').item.json.archivo; } catch (e) {}
let datos = {};
try { datos = $('Parsear respuesta IA').item.json; } catch (e) {}
const err = $json.error;
const detalle = typeof err === 'string' ? err : (err && (err.message || err.description)) || JSON.stringify($json).slice(0, 300);
return { json: { fecha: $now.toISO(), archivo, numero_factura: datos.numero_factura || null, ruc: datos.ruc || null,
  estado: 'error', paso: $prevNode.name, detalle, mensaje: `Error tecnico en ${$prevNode.name}: no se registra ni se avisa a Compras (reintentar).` } };
'''

log_code = r'''
// Una linea JSON por factura (trazabilidad / auditoria)
const lineas = $input.all().map(i => JSON.stringify(i.json)).join('\n') + '\n';
const resumen = $input.all().map(i => `${i.json.archivo}: ${i.json.estado}`).join(' | ');
return [{ json: { resumen, facturas: $input.all().length },
  binary: { data: { data: Buffer.from(lineas, 'utf8').toString('base64'), mimeType: 'application/x-ndjson', fileName: 'facturas.jsonl' } } }];
'''

def http_get(name, path, pos):
    return {"parameters": {"url": "=" + ERP + path, "authentication": "genericCredentialType",
            "genericAuthType": "httpBearerAuth",
            "options": {"response": {"response": {"fullResponse": True, "neverError": True}}, "timeout": 15000}},
            "name": name, "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": pos,
            "credentials": {"httpBearerAuth": BEARER}, "retryOnFail": True, "maxTries": 3, "waitBetweenTries": 2000}

nodes = [
 {"parameters": {"formTitle": "Recepcion de facturas de proveedores",
   "formDescription": "Sube el PDF de la factura. Se extraen los datos con IA, se validan contra el ERP y se registra o se avisa a Compras.",
   "formFields": {"values": [{"fieldLabel": "Factura PDF", "fieldType": "file", "multipleFiles": True, "acceptFileTypes": ".pdf", "requiredField": True}]},
   "responseMode": "lastNode", "options": {}},
  "name": "1. Recibir PDF (formulario)", "type": "n8n-nodes-base.formTrigger", "typeVersion": 2.2, "position": [0, 300], "webhookId": "recepcion-facturas-form"},
 {"parameters": {"jsCode": norm_code}, "name": "Normalizar archivo", "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [220, 300]},
 {"parameters": {"operation": "pdf", "binaryPropertyName": "data", "options": {}}, "name": "Leer texto del PDF", "type": "n8n-nodes-base.extractFromFile", "typeVersion": 1, "position": [440, 300]},
 {"parameters": {"jsCode": prep_code}, "name": "Preparar prompt IA", "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [660, 300]},
 {"parameters": {"method": "POST", "url": "https://api.anthropic.com/v1/messages", "authentication": "genericCredentialType",
   "genericAuthType": "httpHeaderAuth", "sendHeaders": True,
   "headerParameters": {"parameters": [{"name": "anthropic-version", "value": "2023-06-01"}]},
   "sendBody": True, "specifyBody": "json", "jsonBody": "={{ JSON.stringify($json.claude) }}", "options": {"timeout": 60000}},
  "name": "2. Extraer datos con Claude", "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [880, 300],
  "credentials": {"httpHeaderAuth": CLAUDE}, "retryOnFail": True, "maxTries": 5, "waitBetweenTries": 3000},
 {"parameters": {"jsCode": parse_code, "mode": "runOnceForEachItem"}, "name": "Parsear respuesta IA", "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [1100, 300]},
 http_get("ERP: consultar proveedor", "/proveedores/{{ encodeURIComponent($json.ruc) }}", [1320, 300]),
 http_get("ERP: consultar OC", "/ordenes-compra/{{ encodeURIComponent($('Parsear respuesta IA').item.json.orden_compra || 'SIN-OC') }}", [1540, 300]),
 {"parameters": {"jsCode": valid_code, "mode": "runOnceForEachItem"}, "name": "Validar reglas de negocio", "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [1760, 300]},
 {"parameters": {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
   "conditions": [{"id": "c1", "leftValue": "={{ $json.valida }}", "rightValue": True, "operator": {"type": "boolean", "operation": "true", "singleValue": True}}], "combinator": "and"}, "looseTypeValidation": True, "options": {}},
  "name": "¿Factura valida?", "type": "n8n-nodes-base.if", "typeVersion": 2.2, "position": [1980, 300]},
 {"parameters": {"method": "POST", "url": ERP + "/facturas", "authentication": "genericCredentialType", "genericAuthType": "httpBearerAuth",
   "sendBody": True, "specifyBody": "json",
   "jsonBody": "={{ JSON.stringify({ruc: $json.ruc, numero_factura: $json.numero_factura, fecha_emision: $json.fecha_emision, orden_compra: $json.orden_compra, subtotal: $json.subtotal, iva: $json.iva, total: $json.total}) }}",
   "options": {"response": {"response": {"fullResponse": True, "neverError": True}}, "timeout": 15000}},
  "name": "3a. Registrar en ERP", "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [2200, 180], "credentials": {"httpBearerAuth": BEARER}},
 {"parameters": {"jsCode": reg_code, "mode": "runOnceForEachItem"}, "name": "Resultado: registrada", "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [2420, 180]},
 {"parameters": {"fromEmail": "avisos-xavier@prueba.local", "toEmail": "avisos-xavier@prueba.local",
   "subject": "=Factura {{ $json.numero_factura }} NO registrada", "emailFormat": "text",
   "text": "=No se pudo registrar la siguiente factura:\n\n  Numero de factura: {{ $json.numero_factura }}\n  RUC del emisor: {{ $json.ruc }}\n\nMotivo(s):\n{{ $json.motivos_texto }}\n\nPor favor revisar con el area de Compras antes de reintentar.\n",
   "options": {}},
  "name": "3b. Avisar a Compras (correo)", "type": "n8n-nodes-base.emailSend", "typeVersion": 2.1, "position": [2200, 420], "credentials": {"smtp": SMTP}},
 {"parameters": {"jsCode": rech_code, "mode": "runOnceForEachItem"}, "name": "Resultado: rechazada", "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [2420, 420]},
 {"parameters": {"jsCode": log_code}, "name": "4. Armar log", "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [2640, 300]},
 {"parameters": {"operation": "write", "fileName": "={{ '/logs/facturas-' + $now.toFormat('yyyy-MM-dd') + '.jsonl' }}", "dataPropertyName": "data", "options": {"append": True}},
  "name": "5. Guardar log (JSONL)", "type": "n8n-nodes-base.readWriteFile", "typeVersion": 1, "position": [2860, 300]},
]
nodes.append({"parameters": {"jsCode": err_code, "mode": "runOnceForEachItem"}, "name": "Resultado: error tecnico", "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [2420, 640]})
CON_ERROR = ["Leer texto del PDF", "2. Extraer datos con Claude", "Parsear respuesta IA", "ERP: consultar proveedor", "ERP: consultar OC", "Validar reglas de negocio", "3a. Registrar en ERP", "Resultado: registrada", "3b. Avisar a Compras (correo)"]
for n in nodes:
    if n["name"] in CON_ERROR:
        n["onError"] = "continueErrorOutput"
order = [n["name"] for n in nodes]
def link(a, b, out=0): return (a, b, out)
links = [link(order[i], order[i+1]) for i in range(9)] + [
  ("¿Factura valida?", "3a. Registrar en ERP", 0), ("3a. Registrar en ERP", "Resultado: registrada", 0),
  ("¿Factura valida?", "3b. Avisar a Compras (correo)", 1), ("3b. Avisar a Compras (correo)", "Resultado: rechazada", 0),
  ("Resultado: registrada", "4. Armar log", 0), ("Resultado: rechazada", "4. Armar log", 0), ("4. Armar log", "5. Guardar log (JSONL)", 0)]
links += [(n, "Resultado: error tecnico", 1) for n in CON_ERROR] + [("Resultado: error tecnico", "4. Armar log", 0)]
conns = {}
for a, b, o in links:
    main = conns.setdefault(a, {"main": []})["main"]
    while len(main) <= o: main.append([])
    main[o].append({"node": b, "type": "main", "index": 0})
wf = {"id": "recepcionFactXs1", "name": "Recepcion automatizada de facturas (Diosmar)", "active": False,
      "nodes": nodes, "connections": conns, "settings": {"executionOrder": "v1", "timezone": "America/Guayaquil"}, "pinData": {}}
json.dump(wf, open("n8n/recepcion-facturas.workflow.json", "w"), ensure_ascii=False, indent=2)
print("ok", len(nodes))

# TISMA IA escolar (pasarela en la nube)

IA para estudiantes de primaria, liceo y UTU con **TISMA como proveedor**: modelos abiertos en servidores propios
(Qwen u otros, y Hope cuando esté listo), cuentas que da la escuela y un tutor que ayuda a aprender sin hacer la tarea.
API compatible con OpenAI: TISMA Asistente y TISMA Code la usan igual que un motor local.

## Decisiones

| Tema | Decisión |
|---|---|
| Modelo | Abierto y propio. Los datos de menores no salen de los servidores de TISMA. |
| Cuentas | Las da la escuela. El docente reparte un código de 8 letras; cada equipo se vincula con un alias (por ejemplo «Sofía, n.º 12»). |
| Qué ve el docente | Solo sus grupos: temas consultados, estudiantes activos y alertas de riesgo (categoría y resumen). **Nunca las conversaciones.** |
| Qué se guarda | Por mensaje: día, cuenta, tema y tokens. Alertas: categoría y un resumen genérico. El texto no se guarda nunca. Las fichas se guardan como HMAC con una clave del servidor. |
| Retención | Uso: 90 días. Temas y alertas: 1 año. Lo viejo se borra solo. |
| Emergencias | Solo ante riesgo de autolesión o abuso. El equipo de cuidado de TISMA recibe un aviso y puede revisar esa conversación para avisar a las autoridades si corresponde. Ver la sección siguiente. |

## Emergencias (equipo de cuidado de TISMA)

**Qué se guarda.** Solo cuando hay señales de autolesión o abuso. El acoso queda en alerta al docente. Se guardan los últimos mensajes de esa conversación, la respuesta y el alias.

**Sellado.** El contenido queda cifrado con la clave pública del equipo de cuidado (X25519 + AES-256-GCM, `python3-cryptography`). La clave privada no está en el servidor: si roban el servidor o la base, no hay nada legible. Cada sello está atado a su emergencia y no se puede pasar a otra.

**Aviso al equipo.** Llega por webhook con número, categoría, institución y hora. Nunca lleva el contenido ni el alias.

**Revisión.** Se hace con la consola (`python3 -m tisma_ia_escolar.consola_emergencias`):
- Quien revisa se identifica.
- Cada «ver» queda en la auditoría antes de mostrar nada.
- La decisión es una de tres: `falso_positivo`, `escuela` o `autoridades`.
- Al resolver, el contenido se borra. Si nadie lo resuelve, se borra a los 30 días. La auditoría se conserva, sin contenido.

**Transparencia.** El estudiante lo sabe desde el principio. Al vincular el equipo recibe este aviso:

> Lo que preguntás no se guarda. Solo si contás que estás en peligro, el equipo de cuidado de TISMA puede leer esa conversación para ayudarte y avisar a quien corresponda.

**Para que funcione de verdad hace falta:**
- Un equipo de cuidado con guardias y tiempos de respuesta definidos. Una emergencia sin revisar no ayuda a nadie.
- Un procedimiento escrito para avisar a las autoridades: 911 ante peligro inmediato y la escuela por su ruta oficial.
- Revisión legal: interés vital del menor, Ley 18.331, términos aceptados por la institución y las familias.

## Seguridad de menores

Las reglas se aplican en el servidor, antes y después del modelo. No dependen de que el modelo obedezca.

**El tutor:**
- Se adapta a la edad: primaria de 6 a 12 años, liceo de 12 a 18.
- Guía con preguntas y pistas, sin resolver la tarea.
- No pide datos personales y avisa si el estudiante los comparte.
- Habla en español rioplatense.

**Lo que no llega al modelo:** contenido sexual, cómo fabricar o conseguir armas y drogas, y retos peligrosos. El servidor responde con una redirección amable. La salida del modelo también se revisa.

**Señales de riesgo** (autolesión, abuso, acoso):
- No se corta la conversación.
- El modelo recibe una indicación de cuidado.
- La respuesta **siempre** lleva las líneas de ayuda oficiales de Uruguay, aunque el modelo falle:
  - Línea de Prevención del Suicidio: 0800 0767 o \*0767.
  - Apoyo emocional: 0800 1920 o \*1920.
  - Emergencias: 911.
- El docente recibe una alerta con el alias del estudiante.

**Línea Azul de INAU:** no se incluye hasta confirmar su número en una fuente oficial.

**Cupos diarios:** 40 preguntas en primaria y 120 en liceo.

## Límites honestos (v0.1)

- **Detección de riesgo:** usa expresiones en español. Prefiere marcar de más (por ejemplo «me voy a matar estudiando») antes que de menos. Falta una segunda capa con un modelo clasificador abierto (Llama Guard o Qwen3Guard, servido en la misma infraestructura), sobre todo para mensajes con faltas o en inglés.
- **Streaming:** esta versión responde sin streaming.
- **Ley 18.331:** antes de usarse con estudiantes reales, hace falta revisión legal de protección de datos personales (Uruguay). Eso incluye el consentimiento de los padres o tutores, el registro de la base ante la URCDP y un acuerdo con cada institución o con Ceibal.
- **Protocolos:** los de cada institución ante una alerta de riesgo tienen que estar definidos con su equipo; la IA solo avisa.

## Despliegue

1. **Modelo:** servir el modelo abierto con una API OpenAI local, por ejemplo vLLM en `127.0.0.1:8000`.
2. **Clave del servidor:**
   ```
   install -d -m 700 /etc/tisma-ia-escolar
   head -c 48 /dev/urandom > /etc/tisma-ia-escolar/pimienta
   chmod 600 /etc/tisma-ia-escolar/pimienta
   ```
3. **Claves de emergencias:** en el equipo del equipo de cuidado, nunca en el servidor:
   ```
   python3 -m tisma_ia_escolar.consola_emergencias generar-claves /ruta/segura/emergencias.key > emergencias.pub
   ```
   Copiar `emergencias.pub` a `/etc/tisma-ia-escolar/` y definir `TISMA_IA_EMERGENCIAS_WEBHOOK`.
4. **Servicio:**
   ```
   python3 -c "from tisma_ia_escolar.servidor import main; main()" --motor http://127.0.0.1:8000/v1 --modelo qwen
   ```
   Escucha solo en `127.0.0.1:8780`. Delante va Caddy o nginx con TLS.
5. **Alta de la escuela:**
   - `tisma-ia-escolar-admin institucion …`
   - `grupo …`
   - `docente …`
   - `codigo <grupo>`

Pruebas: `cd nube/ia-escolar && python3 -m pytest -q tests`

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
3. **Servicio:**
   ```
   python3 -c "from tisma_ia_escolar.servidor import main; main()" --motor http://127.0.0.1:8000/v1 --modelo qwen
   ```
   Escucha solo en `127.0.0.1:8780`. Delante va Caddy o nginx con TLS.
4. **Alta de la escuela:**
   - `tisma-ia-escolar-admin institucion …`
   - `grupo …`
   - `docente …`
   - `codigo <grupo>`

Pruebas: `cd nube/ia-escolar && python3 -m pytest -q tests`

# Crypto Predictor — Tutorial

App educativa que predice el precio de criptomonedas al cierre de cada hora UTC
y compara contra Kalshi (mercado real). **Sin dinero real**, sin apuestas — sólo
señales y aprendizaje sobre incertidumbre cuantificada.

Símbolos soportados: **BTC, ETH, XRP, DOGE, SOL**. Sólo BTC tiene mercado Kalshi
(serie KXBTCD, horaria); el resto se predice "ciego" contra el modelo.

URL local: `http://100.122.62.70:8001` (Tailscale)

---

## 1 · Modelo en una línea

> El precio en `T+h` minutos se modela como **log-Student-t df=4** con
> deriva cero. La volatilidad por minuto (σ\_1m) se estima con **EWMA λ=0.97**
> sobre los últimos candles de 1 minuto. La σ del horizonte completo se escala
> con √h.

**¿Qué implica "deriva cero"?**
La mediana del modelo es siempre el precio actual. El modelo no predice
dirección — solo dispersión. Si tiene que apostar a si BTC sube o baja en una
hora, la respuesta neutra es 50/50. La señal direccional, si la quieres,
viene del **momentum** (sección 4.3), no del modelo base.

**¿Por qué Student-t df=4 y no Normal?**
Los precios de cripto tienen colas más gordas que la normal. Con t-df=4 el
modelo asigna más probabilidad a eventos extremos (movimientos de ±2σ o más),
lo cual mejora el Brier y baja la frecuencia de "sorpresas" (ver `/calibration`,
sección 5.3).

---

## 2 · Página principal `/?symbol=BTCUSDT`

Cada símbolo tiene la misma estructura. Refresh manual cada ~30 s; la consulta
de threshold (sección 2.2) se actualiza vía JS cada **1 s**.

### 2.1 Cabecera

```
BTCUSDT · cierre 14:00 UTC
[ BTC ] [ ETH ] [ XRP ] [ DOGE ] [ SOL ]
Modelo: log-Student-t df=4 con drift cero. σ_1m via EWMA λ=0.97 sobre N candles 1m.
Refresh 30s. Última obs UTC YYYY-MM-DD HH:MM. Horizonte hasta cierre: XX.X min.
```

- **target HH:00 UTC**: la próxima hora en punto. Cuando se cruza, el modelo
  re-apunta automáticamente a la siguiente.
- **horizonte**: minutos restantes hasta el cierre. Importante para entender
  por qué σ se hace más pequeña a medida que se acerca XX:00.

### 2.2 Consulta — hasta 3 thresholds

Caja con tres inputs (`t1`, `t2`, `t3`). Puedes escribir precios absolutos
(`81500`) o relativos (`+0.5%`, `-100`). Para cada threshold se muestra:

| panel | qué significa |
|---|---|
| **YES (>)** | Nuestra P(precio al cierre > threshold) |
| **NO (≤)** | 1 − YES |
| **KALSHI** | P implícita por el precio del mercado Kalshi (solo BTC). Interpola entre strikes |
| **edge** | YES − Kalshi, en pp. Positivo = nosotros vemos más probable que el mercado |

Fila meta: precio actual, **Δ%** (cuánto está el threshold del spot), **z**
(cuántas σ de distancia), **σ\_h** (sigma del horizonte completo).

> Regla de lectura rápida: `|z|<1` cerca del precio → ~50/50; `|z|≈1` → ~30/70;
> `|z|≈2` → ~15/85; `|z|>3` → cola, <5%.

### 2.3 Precio actual

Hero numérico con **σ del horizonte** y **σ por minuto**. Útil para ver si la
vol está alta o baja en términos absolutos (BTC suele rondar 0.3–0.7 %/h).

### 2.4 Proyección 15 / 30 / 60 min con fan chart

Tarjeta añadida 2026-05-12. Muestra:

- **SVG fan**: dos bandas concéntricas (p10–p90 ancha, p25–p75 estrecha)
  alrededor del precio actual. La línea amarilla discontinua dentro es la
  mediana (= precio actual). La línea sólida es la trayectoria si el
  **momentum reciente** se mantuviera constante.
- **Tabla**: por horizonte (+15, +30, +60 min) muestra el rango p25–p75,
  el rango p10–p90, el momentum proyectado, y una **señal**:

| señal | interpretación |
|---|---|
| dentro p25-p75 | momentum compatible con vol normal; sin sesgo direccional |
| sobre p75 / bajo p25 | tendencia algo fuerte; alerta amarilla |
| rompe p90 / rompe p10 | la tendencia reciente excede lo que la vol normal explicaría; sesgo direccional fuerte (alerta roja) |

**Importante**: la mediana del modelo NO se mueve con el momentum.
El momentum es información complementaria — si la tendencia es fuerte y
persistente, la mediana del modelo está probablemente sesgada hacia abajo
(o arriba). Usa esta señal para decidir si vale la pena fiarse del modelo
o esperar a que la EWMA absorba el cambio.

### 2.5 Velas 1 m

Gráfica de los últimos 60 minutos. Verde = cierre ≥ apertura; rojo = cierre <
apertura.

### 2.6 Bandas predichas al cierre

Histograma horizontal con marcas en p05, p25, p50, p75, p95. La línea amarilla
es el precio actual. Si sale del rango p05–p95, sería un evento ≲5 % por cola.

### 2.7 Cuantiles al cierre

Cinco números: p05 / p25 / p50 / p75 / p95. Tu intervalo de confianza 90 % es
p05 → p95. Tu intervalo más probable (50 %) es p25 → p75.

---

## 3 · `/hourly-call` — point-call horario

A cada hora en punto el sistema publica un **número decimal** tal que BTC tiene
~70 % de probabilidad de **NO sobrepasarlo** al cierre de la próxima hora.
(`p70`: cuantil 70 % de la distribución predicha.)

### 3.1 Card principal

- **Hero**: el valor de la call (`call_value`).
- **Precio al hacer la call**, **σ del horizonte** y **spread implícito**
  (cuánto está la call por encima del precio actual, en %).

### 3.2 Métricas

- **Racha**: número de horas consecutivas en que el precio real ≤ call.
- **Tasa empírica**: hits / settled. **Objetivo: 70 %**. Si la tasa cae a
  ~60 % o menos = el modelo subestima la cola alta (revisar λ o df).

### 3.3 Edge vs Kalshi

Para el strike de Kalshi más cercano a la call:

| columna | qué es |
|---|---|
| **Kalshi NO** | Probabilidad implícita por Kalshi de que BTC ≤ strike al cierre |
| **nuestra NO** | Probabilidad que da nuestro modelo (debe rondar 70 % por construcción) |
| **edge** | nuestra − Kalshi, en pp. **Positivo = el modelo cree que NO es más probable de lo que el mercado precia.** |

Si Kalshi tiene un quote interpolado en el strike exacto de nuestra call,
también lo muestra.

### 3.4 Historial

Tabla con las últimas 30 calls — cierre, precio al momento, call hecha,
precio real al cierre, Δ$, edge, y resultado (✓ win / ✗ loss / pend).

---

## 4 · `/calibration` — ¿está bien calibrado el modelo?

Toda la página vive de **predicciones settleadas** (después de que pase su
cierre y comparemos contra Binance). Las métricas tardan ~24 h en estabilizarse
para un símbolo nuevo.

### 4.1 Brier global

Promedio de `(P − outcome)²` para todos los pares (predicción, resultado).
- **0** = predicciones perfectas
- **0.25** = random coin flip
- Buen modelo cripto: típicamente **0.15–0.20**.

### 4.2 Últimas N horas cerradas

Para cada cierre: predicción más temprana de esa hora (lead alto), precio real,
Δ%, σ%, **|z|**, P(≥real).

- **|z|**: cuántas σ se movió el precio respecto a la predicción.
  Si el modelo está calibrado, **media de |z| ≈ 0.8** (E|Z| para una t).
- **P(≥real)**: si el modelo es correcto, este valor está **uniformemente
  distribuido** entre 0 y 100 % a lo largo de muchas horas (test PIT).
- Colores: |z|>2 rojo (sorpresa), |z|<1 verde (predicción razonable).

### 4.3 Vs Kalshi (solo BTC)

Compara Brier por threshold del modelo vs Kalshi, y cuenta cuántas veces
"gana" cada uno cuando las probabilidades divergen >X pp. **El modelo no
necesita ganar a Kalshi para ser útil** — usalo para detectar cuándo nuestra
predicción es marcadamente diferente del consenso.

### 4.4 Colas

Tabla `|z| > k` observada vs esperada bajo t-df=4. Si el ratio observed/expected
> 1.5× en varios niveles, **σ está subestimada** (subir λ o bajar df).

### 4.5 Top shocks

Las N predicciones con mayor |z|. Útil para hacer post-mortem de eventos
raros (caída/subida fuerte, news…).

### 4.6 Reliability por bucket

Bucketiza P predicha en intervalos (0–10 %, 10–20 %, …, 90–100 %) y
compara la P media de cada bucket contra la frecuencia real de ocurrencia.
- Bucket bien calibrado: |P̄ − frec| < 0.05.
- Gap rojo: en ese rango el modelo está sesgado.

---

## 5 · `/history?symbol=BTCUSDT&target=<unix>` — zoom de una hora

Llegas aquí pinchando el cierre en `/calibration`. Muestra:

- Precio real al cierre (o "aún no settleado").
- Todas las predicciones registradas para esa hora — cuándo se hicieron
  (`made UTC`), cuánto faltaba al cierre (`lead`), el precio en ese momento,
  σ%, Δ% vs real, y P(≥real).

Útil para ver cómo evolucionó la predicción a lo largo de la hora: si σ creció
rápido cerca del cierre = EWMA captó vol elevada.

---

## 6 · APIs JSON (uso programático)

Endpoints sin auth, mismo origen:

```
GET /api/query?symbol=BTCUSDT&t1=81500&t2=82000
  → JSON con now_price, σ%, target_at, queries[].p_above, .kalshi_p, .edge_pct

GET /candles?symbol=BTCUSDT&limit=60
  → JSON con velas 1m: time, open, high, low, close
```

---

## 7 · Cómo tomar decisiones

**No es una recomendación financiera.** Pero como ayuda mental:

1. **Antes de mirar el modelo, fíjate en σ\_h.** Si es alta (>0.7 %/h) la
   incertidumbre es grande y casi nada está "seguro".
2. **Mira el momentum**. Si el rango proyectado +60 min está dentro de p25-p75,
   no hay señal direccional fuerte. Si rompe p90/p10, hay tendencia
   significativa que el modelo aún no incorporó.
3. **Compara con Kalshi cuando exista quote.** Edge ≥5 pp y consistente entre
   thresholds vecinos = el mercado y nosotros estamos contando una historia
   distinta. Investiga (¿news, halving, regulatorio?) antes de confiar ciegamente.
4. **Revisa `/calibration` periódicamente.** Si Brier global sube, o las colas
   están desinfladas, el modelo necesita ajuste antes de usarlo en serio.
5. **No "perseguir" un horizonte de 1 h**. Si quieres tomar una decisión
   discreta (hold/wait/etc.), el horizonte natural del modelo es **hasta
   XX:00**. Forzar predicciones a 5 min con un modelo calibrado a 60 min
   degrada la calidad.

---

## 8 · Limitaciones honestas

- **No incluye fees, slippage, ni microestructura.** Las P son sobre el
  precio mark de Binance al cierre, no sobre lo que ejecutarías.
- **Sin información fundamental.** El modelo es 100 % estadístico — no sabe
  de noticias, halving, ETF flows, ni twitter sentiment.
- **EWMA se adapta con lag.** Tras un cambio de régimen (vol shock), σ_1m
  tarda 30–60 min en re-calibrarse. Durante ese rato las colas estarán
  subestimadas.
- **Kalshi puede tener spreads anchos.** Cuando Kalshi tiene poco volumen, su
  "P implícita" es ruidosa. Ignora el edge cuando la mid-quote es <1 ¢ o >99 ¢.

---

## 9 · Cheatsheet final

| quieres saber… | mira… |
|---|---|
| ¿Probabilidad de BTC>X a HH:00? | `/?symbol=BTCUSDT&t1=X` |
| ¿Probabilidad a corto (15–60 min)? | tarjeta de proyección en `/` |
| ¿Hay tendencia direccional fuerte? | columna **señal** en tabla proyección |
| ¿Cuánto error suelo tener? | `/calibration` → media |z| y Brier |
| ¿Mi point-call de hoy? | `/hourly-call` |
| ¿Qué pasó en una hora concreta? | `/history?symbol=BTCUSDT&target=…` |

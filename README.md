# Bot Telegram - Monitor de productos Panini

Bot que revisa cada 5 minutos tres sitios y te avisa por Telegram cuando:

- Aparece un **producto nuevo**.
- Un producto que estaba **agotado vuelve al stock**.
- Un producto **cambia de precio**.

Sitios monitoreados:

1. https://zonakids.com/
2. https://tiendapanini.com.ar/
3. https://www.mercadolibre.com.ar/tienda/panini

Corre gratis usando **GitHub Actions** — no necesitás servidor propio ni dejar la PC prendida.

---

## 0) Requisitos previos (todo gratis)

- Cuenta de GitHub (https://github.com/signup).
- Cuenta de Telegram.
- El bot y chat_id de Telegram ya creados (ver abajo).

## 1) Crear el bot de Telegram

1. En Telegram, abrí un chat con **@BotFather**.
2. Enviá `/newbot`.
3. Elegí un nombre (ej: `Monitor Panini`) y un username que termine en `bot` (ej: `mipanini_bot`).
4. BotFather te devuelve un **token** tipo `7234567890:AAH...`. **Copialo a un lugar seguro**.
   - Si ya habías creado un bot y compartiste el token por error, enviale `/revoke` a BotFather y generá uno nuevo.

## 2) Conseguir tu chat_id

1. En Telegram, buscá **@userinfobot**.
2. Enviale cualquier mensaje.
3. Te responde con tu **Id** (un número largo). Ese es tu `chat_id`.
4. Enviale un mensaje cualquiera a tu bot (por ejemplo `/start`). Esto es importante para que tu bot tenga permiso de escribirte.

## 3) Subir este proyecto a GitHub

Opción fácil (desde la web, sin consola):

1. Entrá a https://github.com/new.
2. Nombre del repo: `monitor-panini` (puede ser público o privado; si es privado gastás minutos del plan gratis, igual sobran).
3. Tildá **Add a README file** y creá el repo.
4. Una vez creado, arriba a la derecha tocá **Add file → Upload files** y subí:
   - `monitor.py`
   - `requirements.txt`
   - `.gitignore`
   - `README.md` (este archivo)
   - La carpeta `.github/workflows/monitor.yml` (GitHub respeta la jerarquía si arrastrás la carpeta).
5. Confirmá con **Commit changes**.

Si preferís consola:

```bash
git clone https://github.com/<TU_USUARIO>/monitor-panini.git
cd monitor-panini
# Copiá aquí los archivos del proyecto
git add .
git commit -m "Setup inicial"
git push
```

## 4) Guardar los secrets en GitHub

1. En tu repo, andá a **Settings → Secrets and variables → Actions → New repository secret**.
2. Creá dos secrets:
   - Nombre: `TELEGRAM_BOT_TOKEN` — Valor: el token de BotFather.
   - Nombre: `TELEGRAM_CHAT_ID` — Valor: tu chat_id numérico.

NUNCA pongas el token dentro del código. Solo en "Secrets".

## 5) Activar Actions y probar

1. Andá a la pestaña **Actions** del repo.
2. Si te pide confirmar habilitar workflows, aceptá.
3. Vas a ver el workflow **Monitor productos**. Tocá sobre él y después en **Run workflow → Run workflow** (botón gris a la derecha) para probarlo a mano sin esperar el cron.
4. La primera corrida **no manda alertas**: solo guarda el estado inicial de los productos. Vas a ver un commit automático llamado "chore: actualizar state.json [skip ci]".
5. En las siguientes corridas (cada 5 min automáticamente) vas a recibir avisos por Telegram cuando haya cambios.

## 6) Qué hace cada archivo

- `monitor.py`: el script principal. Baja las 3 páginas, extrae productos, compara con `state.json` y envía a Telegram.
- `state.json`: lo crea y actualiza el bot solo. Es la "memoria" del bot (qué productos ya vio).
- `requirements.txt`: dependencias Python.
- `.github/workflows/monitor.yml`: configuración de GitHub Actions (cuándo y cómo correr el script).

## 7) Ajustes frecuentes

- **Cambiar la frecuencia**: editá en `.github/workflows/monitor.yml` el valor `cron: "*/5 * * * *"`. GitHub acepta mínimo 5 minutos. Para cada 15 min usá `"*/15 * * * *"`.
- **Agregar más páginas**: en `monitor.py`, editá la lista `SITES` agregando otra línea con el mismo formato. Si son sitios Magento o Mercado Libre reusás los parsers; si son otros, avisame y te armo el parser.
- **Cortar las alertas**: en GitHub, pestaña **Actions → Monitor productos → ... → Disable workflow**.

## 8) Troubleshooting

- **No me llega ningún mensaje**: asegurate de haberle mandado `/start` a tu bot desde tu cuenta (si no, Telegram bloquea los mensajes del bot hacia vos).
- **Error en el workflow**: abrí la pestaña **Actions** y cliqueá sobre la corrida fallida para ver el log. Los errores típicos son: secrets mal cargados, o que el sitio cambió su HTML (me podés pedir que actualice el parser).
- **El sitio me bloquea (HTTP 403)**: puede pasar si el sitio detecta muchos pedidos. Subí el cron a 15 o 30 min.

## 9) Costos

- GitHub Actions en repos **públicos** es **totalmente gratis**.
- En repos **privados**, el plan gratuito incluye 2000 minutos/mes. Este workflow usa ~30 segundos por corrida × 12 corridas/hora × 24 × 30 = ~4 horas/mes → sobran minutos. Aun así, si querés cero riesgo, hacé el repo público (no hay nada sensible, el token está en secrets).

## 10) Seguridad

- El `state.json` NO contiene el token. Solo guarda nombres, precios y IDs de productos.
- Nunca pegues el token en chats, issues, screenshots o commits.
- Si sospechás que se filtró, `/revoke` en BotFather y actualizá el secret `TELEGRAM_BOT_TOKEN` en GitHub.
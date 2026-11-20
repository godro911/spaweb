"""Petit serveur web pour contrôler un spa Gecko in.touch 2 via geckolib (local, sans cloud)."""

import asyncio
import hmac
import logging
import os
import uuid
from pathlib import Path

from aiohttp import web
from geckolib import GeckoAsyncSpaMan, GeckoSpaEvent, GeckoSpaState

SPA_ADDRESS = os.environ.get("SPA_ADDRESS") or None  # IP du module in.touch 2
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
USER = os.environ.get("SPA_WEB_USER", "spa")
PASSWORD = os.environ.get("SPA_WEB_PASSWORD", "")
STATIC = Path(__file__).parent / "static"
ID_FILE = Path(os.environ.get("CLIENT_ID_FILE", "client_id.txt"))

log = logging.getLogger("spaweb")

REMINDER_LABELS = {
    "RINSE_FILTER": "Rincer le filtre",
    "CLEAN_FILTER": "Nettoyer le filtre",
    "CHANGE_WATER": "Changer l'eau",
    "CHECK_SPA": "Vérifier le spa",
    "CHANGE_OZONATOR": "Changer l'ozonateur",
    "CHANGE_VISION_CARTRIDGE": "Changer la cartouche Vision",
}
# Codes d'alerte ignorés (fausses alertes confirmées contre l'application Gecko).
# Modifiable avec SPA_IGNORE_ALERTS=code1,code2 ; SPA_IGNORE_ALERTS= (vide) affiche tout.
IGNORED_ALERTS = {
    c.strip().lower()
    for c in os.environ.get("SPA_IGNORE_ALERTS", "OverTemp,VSP1CommLost,VSP2CommLost").split(",")
    if c.strip()
}
NO_ERRORS = {"", "None", "No errors or warnings"}  # états « aucune alerte » de geckolib


def client_id() -> str:
    """UUID persistant qui identifie ce client auprès du module."""
    if ID_FILE.exists():
        return ID_FILE.read_text().strip()
    new = str(uuid.uuid4())
    ID_FILE.write_text(new)
    return new


class SpaMan(GeckoAsyncSpaMan):
    async def handle_event(self, event: GeckoSpaEvent, **kwargs) -> None:
        log.debug("event %s", event)


def facade(app: web.Application):
    spaman: SpaMan = app["spaman"]
    if spaman.spa_state != GeckoSpaState.CONNECTED or spaman.facade is None:
        raise web.HTTPServiceUnavailable(text="Spa non connecté")
    return spaman.facade


def snapshot(app: web.Application) -> dict:
    spaman: SpaMan = app["spaman"]
    out = {"connected": False, "state": str(spaman.spa_state)}
    if spaman.spa_state != GeckoSpaState.CONNECTED or spaman.facade is None:
        return out
    f = spaman.facade
    wh = f.water_heater
    out.update(
        connected=True,
        name=f.name,
        temperature={
            "current": wh.current_temperature,
            "target": wh.target_temperature,
            "unit": wh.temperature_unit,
            "min": wh.min_temp,
            "max": wh.max_temp,
            "operation": wh.current_operation,
        },
        pumps=[{"name": p.name, "mode": p.mode, "modes": p.modes} for p in f.pumps],
        blowers=[{"name": b.name, "mode": b.mode, "modes": b.modes} for b in f.blowers],
        lights=[{"name": li.name, "on": li.is_on} for li in f.lights],
        watercare={"mode": f.water_care.state, "modes": f.water_care.states},
        maintenance=maintenance(f),
        alerts=alerts(f),
    )
    return out



def maintenance(f) -> list[dict]:
    """Rappels d'entretien du spa pack (jours restants; négatif = en retard)."""
    rm = f.reminders_manager
    if not rm.is_available:
        return []
    return [
        {
            "type": r.reminder_type.name,
            "label": REMINDER_LABELS.get(r.reminder_type.name, r.description),
            "days": r.days,
        }
        for r in rm.reminders
    ]


def alerts(f) -> list[str]:
    """Erreurs et avertissements actifs signalés par le spa pack."""
    try:
        state = f.error_sensor.state
    except Exception:  # capteur absent selon le modèle de pack
        return []
    if state in NO_ERRORS:
        return []
    found = [e.strip() for e in state.split(",")]
    return [e for e in found if e.lower() not in IGNORED_ALERTS]


async def get_state(request: web.Request) -> web.Response:
    return web.json_response(snapshot(request.app))


def pick(items: list, request: web.Request):
    try:
        return items[int(request.match_info["idx"])]
    except (ValueError, IndexError):
        raise web.HTTPNotFound(text="Appareil inconnu")


async def body(request: web.Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="JSON invalide")
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text="JSON invalide")
    return data


async def set_pump(request: web.Request) -> web.Response:
    dev = pick(facade(request.app).pumps, request)
    mode = (await body(request)).get("mode")
    if mode not in dev.modes:
        raise web.HTTPBadRequest(text=f"Mode invalide, choix: {dev.modes}")
    await dev.async_set_mode(mode)
    return web.json_response({"ok": True})


async def set_blower(request: web.Request) -> web.Response:
    dev = pick(facade(request.app).blowers, request)
    mode = (await body(request)).get("mode")
    if mode not in dev.modes:
        raise web.HTTPBadRequest(text=f"Mode invalide, choix: {dev.modes}")
    await dev.async_set_mode(mode)
    return web.json_response({"ok": True})


async def set_light(request: web.Request) -> web.Response:
    dev = pick(facade(request.app).lights, request)
    on = (await body(request)).get("on")
    if not isinstance(on, bool):
        raise web.HTTPBadRequest(text="'on' doit être true ou false")
    await (dev.async_turn_on() if on else dev.async_turn_off())
    return web.json_response({"ok": True})


async def set_setpoint(request: web.Request) -> web.Response:
    wh = facade(request.app).water_heater
    value = (await body(request)).get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise web.HTTPBadRequest(text="'value' doit être un nombre")
    if not wh.min_temp <= value <= wh.max_temp:
        raise web.HTTPBadRequest(text=f"Hors limites ({wh.min_temp}–{wh.max_temp})")
    await wh.async_set_target_temperature(float(value))
    return web.json_response({"ok": True})


async def set_watercare(request: web.Request) -> web.Response:
    wc = facade(request.app).water_care
    mode = (await body(request)).get("mode")
    if mode not in wc.states:
        raise web.HTTPBadRequest(text=f"Mode invalide, choix: {wc.states}")
    await wc.async_set_state(mode)
    return web.json_response({"ok": True})


@web.middleware
async def auth(request: web.Request, handler):
    header = request.headers.get("Authorization", "")
    if header.startswith("Basic "):
        import base64

        try:
            user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
        except Exception:
            user = pw = ""
        if hmac.compare_digest(user, USER) and hmac.compare_digest(pw, PASSWORD):
            return await handler(request)
    return web.Response(
        status=401, headers={"WWW-Authenticate": 'Basic realm="Spa"'}, text="Authentification requise"
    )


async def index(_: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "index.html")


async def supervise(spaman: SpaMan) -> None:
    """Découvre le spa puis s'y connecte; geckolib gère ensuite les reconnexions."""
    while True:
        try:
            await asyncio.wait_for(spaman.wait_for_descriptors(), timeout=30)
            descs = spaman.spa_descriptors
            if descs:
                d = descs[0]
                log.info("Spa trouvé: %s à %s", d.name, d.ipaddress)
                await spaman.async_set_spa_info(d.ipaddress, d.identifier_as_string, d.name)
                await spaman.wait_for_facade()
                return
            log.warning("Aucun spa trouvé, nouvel essai dans 15 s")
        except asyncio.TimeoutError:
            log.warning("Découverte sans réponse, nouvel essai")
        await asyncio.sleep(15)
        await spaman.async_reset()


async def on_startup(app: web.Application) -> None:
    spaman = SpaMan(client_id(), spa_address=SPA_ADDRESS)
    await spaman.__aenter__()
    app["spaman"] = spaman
    app["supervisor"] = asyncio.create_task(supervise(spaman))


async def on_cleanup(app: web.Application) -> None:
    app["supervisor"].cancel()
    await app["spaman"].__aexit__(None, None, None)


def make_app() -> web.Application:
    app = web.Application(middlewares=[auth] if PASSWORD else [])
    app.add_routes(
        [
            web.get("/", index),
            web.get("/api/state", get_state),
            web.post("/api/pump/{idx}", set_pump),
            web.post("/api/blower/{idx}", set_blower),
            web.post("/api/light/{idx}", set_light),
            web.post("/api/setpoint", set_setpoint),
            web.post("/api/watercare", set_watercare),
        ]
    )
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    if not PASSWORD and os.environ.get("ALLOW_NO_AUTH") != "1":
        raise SystemExit("Définis SPA_WEB_PASSWORD (ou ALLOW_NO_AUTH=1 pour tester en local).")
    web.run_app(make_app(), host=HOST, port=PORT)

"""DGT cita previa watcher: Barcelona, "Inscripcion, sustitucion y renovacion de permisos europeos".

Walks the public booking flow over plain HTTP (no login, no captcha on these steps) and pushes
an ntfy alert to the phone when the office stops showing "sin capacidad de citacion".
Booking itself is done by hand.

Usage:
  python watcher.py           one check (what the scheduled task runs)
  python watcher.py --test    send a test push
"""
import json, os, re, sys, time, datetime, pathlib, html
import requests

HERE = pathlib.Path(__file__).resolve().parent
_cfg_file = HERE / "config.json"
CFG = json.loads(_cfg_file.read_text(encoding="utf-8")) if _cfg_file.exists() else {}
if os.environ.get("NTFY_TOPIC"):
    CFG["ntfy_topic"] = os.environ["NTFY_TOPIC"]
IN_CI = os.environ.get("GITHUB_ACTIONS") == "true"
STATE_FILE = HERE / "state.json"
LOG_FILE = HERE / "watcher.log"

HOST = "https://sedeclave.dgt.gob.es"
INICIO = HOST + "/WEB_CITE_CONSULTA/paginas/inicio.faces"
CATALOGO = HOST + "/WEB_CITE_CONSULTA/paginas/catalogo.faces"
BOOK_URL = "https://sede.dgt.gob.es/.galleries/enlaces/enlaces_sedeapl/CITE-cita-previa.html"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"

OFFICE = CFG.get("office_id", "545")      # Jefatura Provincial de Trafico de Barcelona
AREA = CFG.get("area", "CNJ")             # Canjes
TRAMITE = CFG.get("tramite_id", "184")    # Inscripcion, sustitucion y renovacion de permisos europeos
REMIND_MIN = CFG.get("remind_every_min", 15)
ERROR_ALERT_AFTER = CFG.get("error_alert_after", 6)


def log(msg):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    # keep log small
    lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
    if len(lines) > 5000:
        LOG_FILE.write_text("\n".join(lines[-3000:]) + "\n", encoding="utf-8")


def push(title, body, priority="default", tags="", click=BOOK_URL):
    r = requests.post(
        f"https://ntfy.sh/{CFG['ntfy_topic']}",
        data=body.encode("utf-8"),
        headers={"Title": title, "Priority": priority, "Tags": tags, "Click": click},
        timeout=20,
    )
    r.raise_for_status()


def viewstate_full(text):
    return re.search(r'name="javax\.faces\.ViewState"[^>]*value="([^"]+)"', text).group(1)


def check():
    """Return ('available'|'none'|'not_configured', detail)."""
    s = requests.Session()
    s.headers["User-Agent"] = UA
    r = s.get(INICIO, timeout=30)
    r.raise_for_status()
    r.encoding = "utf-8"
    vs = viewstate_full(r.text)
    form = re.search(r'(?s)<form id="formselectorCentro".*?</form>', r.text).group(0)
    centro = re.search(r'<select id="(formselectorCentro:[^"]+)"', form).group(1)

    def ajax(src, fields, event):
        nonlocal vs
        data = {"formselectorCentro_SUBMIT": "1", "javax.faces.ViewState": vs,
                "javax.faces.source": src, "javax.faces.partial.event": "change",
                "javax.faces.partial.execute": src, "javax.faces.partial.render": "formselectorCentro",
                "javax.faces.behavior.event": event, "javax.faces.partial.ajax": "true", **fields}
        x = s.post(INICIO, data=data, timeout=30, headers={
            "Faces-Request": "partial/ajax", "X-Requested-With": "XMLHttpRequest", "Referer": INICIO})
        x.raise_for_status()
        x.encoding = "utf-8"
        m = re.search(r'ViewState[^>]*><!\[CDATA\[([^\]]+)', x.text)
        if m:
            vs = m.group(1)
        return x.text

    t = ajax(centro, {centro: OFFICE}, "change")
    area_sel = re.search(r'<select id="(formselectorCentro:idAreaSelector)"', t).group(1)
    t = ajax(area_sel, {centro: OFFICE, area_sel: AREA}, "valueChange")
    button = re.search(r'<button id="(formselectorCentro:[^"]+)"[^>]*type="submit"', t).group(1)

    x = s.post(INICIO, timeout=30, headers={"Referer": INICIO}, data={
        centro: OFFICE, area_sel: AREA, "formselectorCentro_SUBMIT": "1",
        "javax.faces.ViewState": vs, button: ""})
    x.raise_for_status()
    x.encoding = "utf-8"
    m = re.search(r"submitForm\('tramites','([^']+)'\);\"[^>]*id=\"seleccionarTramitea_" + TRAMITE + '"', x.text)
    if not m:
        raise RuntimeError(f"tramite {TRAMITE} not listed in area {AREA}")

    y = s.post(CATALOGO, timeout=30, headers={"Referer": x.url}, data={
        "tramites_SUBMIT": "1", "javax.faces.ViewState": viewstate_full(x.text), "tramites:_idcl": m.group(1)})
    y.raise_for_status()
    y.encoding = "utf-8"
    page = y.text
    if "Selecciona fecha y hora" not in page:
        raise RuntimeError(f"unexpected page {y.url} ({len(page)} bytes)")
    if re.search(r"PF\('sinCapacidad'\)\.show\(\)", page):
        return "none", "sin capacidad de citacion"
    if re.search(r"PF\('noConfigurado'\)\.show\(\)", page):
        return "not_configured", "no configurada para el tramite"
    # Slots shown: grab any visible day numbers/dates as a hint for the push text
    cal = page[page.find("cite-calendario"):]
    cal = re.sub(r"(?s)<style.*?</style>|<script.*?</script>", " ", cal)
    hint = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", cal)))[:300]
    return "available", hint


def main():
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    now = time.time()
    try:
        status, detail = check()
    except Exception as e:  # noqa: BLE001
        state["errors"] = state.get("errors", 0) + 1
        log(f"ERROR #{state['errors']}: {e!r}")
        if state["errors"] == ERROR_ALERT_AFTER:
            push("DGT watcher broken", f"{ERROR_ALERT_AFTER} failed checks in a row. Last error: {e!r}"[:500],
                 priority="default", tags="warning")
        STATE_FILE.write_text(json.dumps(state))
        return
    if state.get("errors", 0) >= ERROR_ALERT_AFTER:
        push("DGT watcher recovered", "Checks are working again.", priority="low", tags="white_check_mark")
    state["errors"] = 0
    log(f"{status}: {detail[:120]}")
    if status == "available":
        last = state.get("last_alert", 0)
        if state.get("status") != "available" or now - last >= REMIND_MIN * 60:
            push("DGT Barcelona: slots OPEN",
                 "EU licence inscription (Inscripcion de permisos europeos) has slots. Book now: "
                 "Barcelona > Canjes > Inscripcion, sustitucion y renovacion de permisos europeos.",
                 priority="urgent", tags="rotating_light,car")
            state["last_alert"] = now
    state["status"] = status
    state["last_check"] = now
    STATE_FILE.write_text(json.dumps(state))


def main_ci():
    """Stateless run for GitHub Actions: retry, push on every run with slots, fail the job if DGT is unreachable."""
    last = None
    for attempt in range(3):
        try:
            status, detail = check()
            print(f"{status}: {detail[:200]}")
            if status == "available":
                push("DGT Barcelona: slots OPEN",
                     "EU licence inscription (Inscripcion de permisos europeos) has slots. Book now: "
                     "Barcelona > Canjes > Inscripcion, sustitucion y renovacion de permisos europeos.",
                     priority="urgent", tags="rotating_light,car")
            return
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"attempt {attempt + 1} failed: {e!r}")
            time.sleep(30)
    sys.exit(f"DGT check failed 3 times: {last!r}")


if __name__ == "__main__":
    if "--test" in sys.argv:
        push("DGT watcher test", "Test push. If you see this, alerts will reach your phone.", tags="test_tube")
        print("test push sent")
    elif "--once" in sys.argv:
        print(check())
    elif IN_CI:
        main_ci()
    else:
        import random
        time.sleep(random.uniform(0, 60))  # jitter so checks don't land on exact minutes
        main()

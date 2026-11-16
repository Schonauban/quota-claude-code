#!/usr/bin/env python3
"""
quota — lecture du quota de l'abonnement Claude et compteur du coût d'un tour,
pour qu'une session Claude Code sache qu'elle approche de la fin du quota et
s'arrête proprement au lieu d'être coupée. Lecture, mesure et alerte seulement :
pas d'arrêt forcé.

  barre   barre d'état de Claude Code (statusLine) : lit le JSON sur stdin
          (quota 5 h et 7 j, contexte), note l'état de la session et la
          hausse du quota depuis le tour précédent, affiche une ligne ;
  alerte  hook UserPromptSubmit et PostToolUse : prévient la session quand il
          ne reste qu'environ 8 tours, puis 2 (en tours mesurés) ;
  etat    état de toutes les sessions de la machine.

Le coût d'un tour est MESURÉ : la plus forte des 5 dernières hausses du quota
entre deux passages de la barre. Le quota est celui du compte : les autres
sessions ouvertes gonflent la hausse, la marge n'en est que plus prudente.
Données : ~/.cache/quota/ (QUOTA_BASE pour les tests). Rien ne sort de la machine.
"""

import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

BASE = Path(os.environ.get("QUOTA_BASE") or (Path.home() / ".cache" / "quota"))
SESSIONS = BASE / "sessions"
ANNONCES = BASE / "annonces"
HISTO = BASE / "historique.jsonl"

COUT_DEFAUT_5H = 1.0          # points de quota 5 h par tour, tant que non mesuré
COUT_DEFAUT_7J = 0.2
COUT_BORNES = (0.2, 30.0)
HAUSSES_RETENUES = 5          # coût d'un tour = la plus forte des 5 dernières hausses
PAUSE_MAX_S = 180             # une hausse plus de 3 min après la précédente n'est pas un tour
ALERTE_TOURS = 8.0            # première alerte
ARRET_TOURS = 2.0             # alerte « arrête-toi maintenant »
CONTEXTE_LOURD = 60.0         # % du contexte
RAPPEL_S = 900                # même niveau : rappel au plus toutes les 15 min
ETAT_PERIME_S = 6 * 3600
ORDRE = {"inconnu": 0, "ok": 0, "alerte": 1, "arret": 2}
_SID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def sid_valide(s):
    return s if isinstance(s, str) and _SID.match(s) else None


def lire_json(chemin):
    try:
        return json.loads(Path(chemin).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def ecrire_json(chemin, donnees):
    chemin = Path(chemin)
    chemin.parent.mkdir(parents=True, exist_ok=True)
    tmp = chemin.with_suffix(".tmp")
    tmp.write_text(json.dumps(donnees, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, chemin)


def lire_fin(chemin, taille=65536):
    try:
        with open(chemin, "rb") as f:
            f.seek(0, 2)
            debut = max(0, f.tell() - taille)
            f.seek(debut)
            lignes = f.read().decode("utf-8", "replace").splitlines()
            return lignes[1:] if debut else lignes   # première ligne coupée seulement au milieu
    except OSError:
        return []


def pct(x):
    return "?" if x is None else "%d %%" % round(x)


def heure(epoch):
    if not epoch:
        return "?"
    t = dt.datetime.fromtimestamp(epoch)
    return t.strftime("%H:%M") if t.date() == dt.date.today() else t.strftime("%d/%m %H:%M")


# -- Compteur : coût mesuré d'un tour ----------------------------------------- #

def noter_hausse(sid, avant, apres):
    """Une hausse du quota entre deux passages de la barre = le coût d'une réponse."""
    ecart = apres["t"] - (avant.get("t") or 0)
    if ecart > PAUSE_MAX_S:
        return                             # après une pause : pas le coût d'un tour
    ligne = {"t": round(apres["t"]), "s": sid, "m": apres.get("modele_id"), "dt": round(ecart)}
    utile = False
    for q, r, d in (("q5", "r5", "d5"), ("q7", "r7", "d7")):
        a, b = avant.get(q), apres.get(q)
        if a is None or b is None or b <= a:
            continue                       # baisse : remise à zéro ou changement de compte
        if abs((avant.get(r) or 0) - (apres.get(r) or 0)) > 300:
            continue                       # changement de fenêtre : pas une hausse
        ligne[d] = round(b - a, 2)
        utile = True
    if not utile:
        return
    try:
        HISTO.parent.mkdir(parents=True, exist_ok=True)
        with open(HISTO, "a", encoding="utf-8") as f:
            f.write(json.dumps(ligne) + "\n")
        if HISTO.stat().st_size > 600_000:  # on garde la seconde moitié
            lignes = HISTO.read_text(encoding="utf-8").splitlines()
            HISTO.write_text("\n".join(lignes[len(lignes) // 2:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def cout_tour(sid, cle, defaut, modele=None):
    """(coût d'un tour, nombre de hausses, source) : cette session d'abord,
    sinon le même modèle dans d'autres sessions, sinon la valeur par défaut."""
    propres, meme = [], []
    for l in reversed(lire_fin(HISTO)):
        try:
            d = json.loads(l)
        except ValueError:
            continue
        v = d.get(cle)
        if not v:
            continue
        if d.get("s") == sid:
            propres.append(float(v))
            if len(propres) >= HAUSSES_RETENUES:
                break
        elif modele and d.get("m") == modele and len(meme) < HAUSSES_RETENUES:
            meme.append(float(v))
    borne = lambda x: min(max(x, COUT_BORNES[0]), COUT_BORNES[1])
    if propres:
        return borne(max(propres)), len(propres), "session"
    if meme:
        return borne(max(meme)), len(meme), "modele"
    return defaut, 0, "defaut"


def evaluer(e, maintenant=None):
    t = maintenant or time.time()
    if not e or t - (e.get("t") or 0) > ETAT_PERIME_S:
        return {"niveau": "inconnu"}
    q5, r5, q7, r7 = e.get("q5"), e.get("r5"), e.get("q7"), e.get("r7")
    if r5 and t >= r5:
        q5 = 0.0                  # fenêtre écoulée : le quota est remis à zéro
    if r7 and t >= r7:
        q7 = 0.0
    c5, n5, s5 = cout_tour(e.get("session"), "d5", COUT_DEFAUT_5H, e.get("modele_id"))
    c7, n7, s7 = cout_tour(e.get("session"), "d7", COUT_DEFAUT_7J, e.get("modele_id"))
    t5 = (100 - q5) / c5 if q5 is not None else float("inf")
    t7 = (100 - q7) / c7 if q7 is not None else float("inf")
    if t5 <= t7:
        tours, fenetre, reset, cout, n, src = t5, "5 h", r5, c5, n5, s5
    else:
        tours, fenetre, reset, cout, n, src = t7, "7 j", r7, c7, n7, s7
    niveau = "arret" if tours <= ARRET_TOURS else "alerte" if tours <= ALERTE_TOURS else "ok"
    ctx = e.get("ctx")
    return {"niveau": niveau, "tours": tours, "fenetre": fenetre, "reset": reset,
            "cout": cout, "n": n, "source": src, "q5": q5, "r5": r5, "q7": q7, "r7": r7,
            "ctx": ctx, "lourd": ctx is not None and ctx >= CONTEXTE_LOURD,
            "session": e.get("session"), "projet": e.get("projet"), "modele": e.get("modele")}


def resume(a):
    mesure = {"session": "mesuré sur %d tour(s) de cette session",
              "modele": "mesuré sur %d tour(s) du même modèle ailleurs",
              "defaut": "pas encore mesuré%.0s"}[a["source"]] % a["n"]
    return ("quota %s utilisé : %s (5 h) et %s (7 j), remise à zéro %s ; ~%d tour(s) "
            "restant(s) à ~%.1f point(s) par tour (%s) ; contexte %s"
            % (a["fenetre"], pct(a["q5"]), pct(a["q7"]), heure(a["reset"]),
               max(0, int(a["tours"])), a["cout"], mesure, pct(a["ctx"])))


# -- Barre d'état ------------------------------------------------------------- #

def barre():
    try:
        d = json.load(sys.stdin)
    except ValueError:
        print("quota : données illisibles")
        return 0
    sid = sid_valide(d.get("session_id"))
    rl = d.get("rate_limits") or {}
    f5, f7 = rl.get("five_hour") or {}, rl.get("seven_day") or {}
    cw = d.get("context_window") or {}
    modele = d.get("model") or {}
    e = {"t": time.time(), "session": sid,
         "projet": (d.get("workspace") or {}).get("project_dir") or d.get("cwd"),
         "modele_id": modele.get("id"), "modele": modele.get("display_name"),
         "q5": f5.get("used_percentage"), "r5": f5.get("resets_at"),
         "q7": f7.get("used_percentage"), "r7": f7.get("resets_at"),
         "ctx": cw.get("used_percentage")}
    if sid:
        chemin = SESSIONS / (sid + ".json")
        noter_hausse(sid, lire_json(chemin) or {}, e)
        try:
            ecrire_json(chemin, e)
        except OSError:
            pass
    a = evaluer(e)
    m = [e["modele"] or e["modele_id"] or "?"]
    if e["q5"] is not None:
        m.append("5 h %s ↻%s" % (pct(e["q5"]), heure(e["r5"])))
    if e["q7"] is not None:
        m.append("7 j %s" % pct(e["q7"]))
    m.append("contexte %s" % pct(e["ctx"]))
    if a.get("niveau") in ("alerte", "arret"):
        m.append("%s ~%d tours" % ("⛔" if a["niveau"] == "arret" else "⚠", max(0, int(a["tours"]))))
    print(" · ".join(m))
    return 0


# -- Alerte (hooks) ------------------------------------------------------------- #

def texte_alerte(a):
    if a["niveau"] == "arret":
        return ("quota — ARRÊTE-TOI MAINTENANT : %s. Ne commence rien de nouveau. Mets le "
                "travail dans un état sûr (pas de modification à moitié faite), consigne où tu "
                "en es et ce qui reste, commite, puis termine ton tour en le disant." % resume(a))
    return ("quota — ALERTE : %s. Termine l'étape en cours jusqu'à un point propre, sans en "
            "commencer une longue ; prépare la reprise (consigner, commiter)." % resume(a))


def annonce(a, g, maintenant=None):
    t, messages = maintenant or time.time(), []
    niveau = a.get("niveau", "inconnu")
    if niveau in ("alerte", "arret"):
        if ORDRE[niveau] > ORDRE.get(g.get("niveau"), 0) or t - g.get("t", 0) > RAPPEL_S:
            messages.append(texte_alerte(a))
            g["niveau"], g["t"] = niveau, t
    elif g.get("niveau") in ("alerte", "arret"):
        g["niveau"] = "ok"            # quota remis à zéro ou autre compte : on pourra réannoncer
    if a.get("lourd") and (not g.get("ctx") or a["ctx"] >= g["ctx"] + 10):
        messages.append("quota — CONTEXTE LOURD : %s du contexte utilisé. Chaque tour coûte plus "
                        "cher : termine la tâche ou consigne-la pour une session neuve." % pct(a["ctx"]))
        g["ctx"] = a["ctx"]
    return "\n".join(messages) or None


def alerte():
    try:
        d = json.load(sys.stdin)
    except ValueError:
        return 0
    sid = sid_valide(d.get("session_id"))
    evenement = d.get("hook_event_name") or ""
    if not sid or evenement not in ("UserPromptSubmit", "PostToolUse"):
        return 0
    chemin_g = ANNONCES / (sid + ".json")
    g = lire_json(chemin_g) or {}
    avant = dict(g)
    texte = annonce(evaluer(lire_json(SESSIONS / (sid + ".json"))), g)
    if g != avant:
        try:
            ecrire_json(chemin_g, g)
        except OSError:
            pass
    if texte:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": evenement,
                                                 "additionalContext": texte}}, ensure_ascii=False))
    return 0


# -- État de toutes les sessions -------------------------------------- #

def etat():
    lignes = []
    for p in sorted(SESSIONS.glob("*.json")):
        a = evaluer(lire_json(p))
        if a["niveau"] == "inconnu":
            continue
        lignes.append("%-7s %-22s %s" % (a["niveau"], Path(a.get("projet") or "?").name, resume(a)))
    print("\n".join(lignes) or "aucune session active (barre d'état pas encore passée)")
    return 0


def main():
    commandes = {"barre": barre, "alerte": alerte, "etat": etat}
    if len(sys.argv) != 2 or sys.argv[1] not in commandes:
        print("usage : quota.py barre|alerte|etat", file=sys.stderr)
        return 2
    try:
        return commandes[sys.argv[1]]()
    except Exception as ex:           # un hook ou une barre en panne ne doit rien bloquer
        print("quota : en panne (%s)" % type(ex).__name__, file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Tests de quota.py, dans un dossier temporaire (QUOTA_BASE). Usage : python3 -I quota/test_quota.py"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ICI = Path(__file__).resolve().parent
SCRIPT = ICI / "quota.py"


def entree(sid="s1", q5=50.0, q7=10.0, ctx=20.0, r5=None, r7=None):
    t = time.time()
    return {"session_id": sid, "model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"},
            "workspace": {"project_dir": "/home/moi/projet"},
            "rate_limits": {"five_hour": {"used_percentage": q5, "resets_at": r5 or t + 3600},
                            "seven_day": {"used_percentage": q7, "resets_at": r7 or t + 86400 * 3}},
            "context_window": {"used_percentage": ctx}}


class Quota(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = dict(os.environ, QUOTA_BASE=self.tmp.name)
        self.r5, self.r7 = time.time() + 3600, time.time() + 86400 * 3

    def tearDown(self):
        self.tmp.cleanup()

    def lancer(self, cmd, donnees):
        r = subprocess.run([sys.executable, "-I", str(SCRIPT), cmd], input=json.dumps(donnees),
                           capture_output=True, text=True, env=self.env, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def barre(self, q5, sid="s1", ctx=20.0):
        return self.lancer("barre", entree(sid, q5=q5, ctx=ctx, r5=self.r5, r7=self.r7))

    def hook(self, sid="s1", evenement="PostToolUse"):
        out = self.lancer("alerte", {"session_id": sid, "hook_event_name": evenement})
        return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""

    def test_barre_affiche_quota_et_contexte(self):
        ligne = self.barre(42)
        self.assertIn("Opus 5.5", ligne)
        self.assertIn("5 h 42 %", ligne)
        self.assertIn("7 j 10 %", ligne)
        self.assertIn("contexte 20 %", ligne)

    def test_hausse_mesuree_par_tour(self):
        self.barre(50)
        self.barre(53)
        histo = (Path(self.tmp.name) / "historique.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(histo[-1])["d5"], 3.0)

    def test_baisse_pas_comptee(self):
        self.barre(80)
        self.barre(10)          # remise à zéro du quota
        self.assertFalse((Path(self.tmp.name) / "historique.jsonl").exists())

    def test_rien_tant_que_loin(self):
        self.barre(50)
        self.barre(51)
        self.assertEqual(self.hook(), "")

    def test_alerte_puis_arret(self):
        self.barre(80)
        self.barre(84)          # 4 points par tour : 16 restants = 4 tours → alerte
        texte = self.hook()
        self.assertIn("ALERTE", texte)
        self.assertEqual(self.hook(), "")      # pas redit tout de suite
        self.barre(92)          # 8 restants à 4 (max des hausses) → 2 tours → arrêt
        self.assertIn("ARRÊTE-TOI", self.hook())
        self.assertIn("⛔", self.barre(92))

    def test_alerte_par_session(self):
        self.barre(80)
        self.barre(84)
        self.assertIn("ALERTE", self.hook())
        self.barre(84, sid="s2")
        self.assertIn("ALERTE", self.hook(sid="s2"))   # coût du même modèle repris

    def test_contexte_lourd(self):
        self.barre(10, ctx=65)
        self.assertIn("CONTEXTE LOURD", self.hook())

    def test_hook_userpromptsubmit(self):
        self.barre(80)
        self.barre(84)
        out = self.lancer("alerte", {"session_id": "s1", "hook_event_name": "UserPromptSubmit"})
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")

    def test_entree_illisible_laisse_passer(self):
        for cmd in ("barre", "alerte"):
            r = subprocess.run([sys.executable, "-I", str(SCRIPT), cmd], input="pas du json",
                               capture_output=True, text=True, env=self.env, timeout=10)
            self.assertEqual(r.returncode, 0)

    def test_session_inconnue_silence(self):
        self.assertEqual(self.hook(sid="jamais-vue"), "")

    def test_etat_liste_les_sessions(self):
        self.barre(30)
        r = subprocess.run([sys.executable, "-I", str(SCRIPT), "etat"], capture_output=True,
                           text=True, env=self.env, timeout=10)
        self.assertIn("projet", r.stdout)
        self.assertIn("30 %", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=1)

# quota-claude-code

Barre d'état et alertes de quota pour Claude Code : une session voit qu'elle approche de la fin du quota de l'abonnement (fenêtres de 5 h et de 7 jours) et s'arrête proprement au lieu d'être coupée au milieu d'un travail.

- **`barre`** (statusLine) : lit le JSON que Claude Code donne à la barre d'état (quota 5 h et 7 j, contexte), affiche une ligne, et mesure la hausse du quota d'un tour à l'autre.
- **`alerte`** (hooks `UserPromptSubmit` et `PostToolUse`) : prévient la session quand il ne reste qu'environ 8 tours (« ALERTE »), puis 2 (« ARRÊTE-TOI »), et quand le contexte devient lourd (60 %).
- **`etat`** : état de toutes les sessions de la machine.

Le coût d'un tour est **mesuré** : la plus forte des 5 dernières hausses du quota entre deux passages de la barre (1 point par défaut tant qu'il n'y a pas de mesure). Le quota est celui du compte : les autres sessions ouvertes gonflent la hausse, la marge n'en est que plus prudente. Lecture, mesure et alerte seulement : rien n'est arrêté de force.

Python 3, bibliothèque standard seulement. Données dans `~/.cache/quota/` (`QUOTA_BASE` pour la changer). Rien ne sort de la machine.

## Installation

Dans `~/.claude/settings.json` (chemin à adapter) :

```json
{
  "statusLine": {"type": "command", "command": "python3 -I /chemin/vers/quota.py barre"},
  "hooks": {
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python3 -I /chemin/vers/quota.py alerte"}]}],
    "PostToolUse": [{"hooks": [{"type": "command", "command": "python3 -I /chemin/vers/quota.py alerte"}]}]
  }
}
```

État de toutes les sessions : `python3 -I quota.py etat`.

## Tests

`python3 -I test_quota.py` (dossier temporaire, rien n'est écrit ailleurs).

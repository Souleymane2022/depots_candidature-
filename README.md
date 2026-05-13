# job-agent

Agent CLI Python autonome qui :

1. **Lit** une offre d'emploi à partir de son URL.
2. **Score** sa pertinence par rapport à ton profil (Claude `opus-4-7`).
3. **Génère** une lettre de motivation personnalisée si le score dépasse un seuil.
4. **Pilote** un navigateur stealth (`patchright` + `browser-use`) pour remplir et soumettre le formulaire.
5. **Tourne** un email de ton pool, sans jamais postuler 2 fois avec la même adresse chez la même entreprise.
6. **Surveille** en IMAP la confirmation de candidature et valide le lien automatiquement.
7. **Stocke** tout dans SQLite et te donne un tableau de bord clair (`status`, `inbox`).

---

## ⚠️ Avertissement & responsabilité

> Les conditions générales de certains sites d'emploi (LinkedIn, Indeed, etc.) **interdisent l'automatisation** des candidatures. L'usage de cet outil sur ces plateformes peut entraîner la **suspension de ton compte** ou des conséquences contractuelles.
>
> Cet outil est fourni à des fins **éducatives** et de **productivité personnelle**. **Tu es seul responsable** de son utilisation : tu dois vérifier la conformité aux CGU de chaque site, à la législation locale (RGPD, droit du travail), et tu assumes l'intégralité des risques.
>
> Aucune garantie, expresse ou implicite, n'est fournie. Cf. la licence MIT (`LICENSE`).

---

## Prérequis

- **Python 3.12+** (`python3 --version`)
- **`uv`** (gestionnaire de paquets rapide)
- **Chromium** (installé automatiquement par Playwright)
- Une **clé API Anthropic** (https://console.anthropic.com/)
- Un **CV PDF**
- Au moins **un compte Gmail** avec un mot de passe d'application

---

## Installation

### 1. Installer `uv`

**macOS / Ubuntu :**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows 11 (PowerShell) :**
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### 2. Cloner le projet et installer les dépendances

```bash
git clone <repo-url> job-agent
cd job-agent

uv python install 3.12     # si Python 3.12 absent
uv sync --extra dev        # installe les deps + les outils de dev
```

### 3. Installer Chromium (utilisé par patchright / browser-use)

```bash
uv run playwright install chromium
```

**Linux uniquement** (dépendances système pour Chromium) :
```bash
sudo uv run playwright install-deps chromium
```

---

## Configuration

### 1. `.env`

Copie le template :

**macOS / Linux :**
```bash
cp .env.example .env
```

**Windows :**
```powershell
copy .env.example .env
```

Édite `.env` et renseigne **uniquement** :

```ini
ANTHROPIC_API_KEY=sk-ant-...
```

> **Obtenir une clé Anthropic** : https://console.anthropic.com/settings/keys

### 2. `data/profile.json`

Copie l'exemple :

```bash
cp data/profile.example.json data/profile.json   # macOS/Linux
copy data\profile.example.json data\profile.json # Windows
```

Édite-le. Champs obligatoires : `first_name`, `last_name`, `phone`, `city`, `country`, `years_experience`, `current_title`, `skills`, `languages`, `preferences`, `cv_path`, `bio_short`.

Champ utile : **`target_roles`** (liste de titres de postes visés). Si présent, il oriente fortement le scoring.

```json
{
  "first_name": "Prénom",
  "last_name": "Nom",
  ...
  "target_roles": ["Backend Engineer", "Platform Engineer"],
  ...
  "preferences": {
    "remote": "hybrid",
    "salary_min_eur": 50000,
    "contract_types": ["CDI"],
    "excluded_industries": ["tobacco", "gambling"]
  },
  "cv_path": "data/cv_base.pdf"
}
```

### 3. `data/emails.json`

```bash
cp data/emails.example.json data/emails.json   # macOS/Linux
copy data\emails.example.json data\emails.json # Windows
```

Pour chaque adresse Gmail, renseigne le **mot de passe d'application** (jamais ton mot de passe principal).

> **Générer un mot de passe d'application Gmail** : https://myaccount.google.com/apppasswords
>
> Le mot de passe d'application Gmail nécessite que la 2FA soit activée sur ton compte Google.

```json
[
  {
    "address": "exemple1@gmail.com",
    "app_password": "abcd efgh ijkl mnop",
    "imap_host": "imap.gmail.com",
    "imap_port": 993,
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,
    "display_name": "Prénom Nom"
  }
]
```

### 4. `data/cv_base.pdf`

Place ton CV PDF de base à `data/cv_base.pdf` (ou ajuste `cv_path` dans `profile.json`).

---

## Commandes CLI

| Commande | Description |
|---|---|
| `uv run job-agent doctor` | Vérifie .env, profile, emails, CV, API Claude, connexions IMAP. |
| `uv run job-agent init` | Crée la DB, importe `emails.json`, valide chaque email en IMAP. |
| `uv run job-agent add <url>` | Ajoute une offre au pipeline (status `pending`). |
| `uv run job-agent add <url> --auto` | Ajoute **et** traite immédiatement l'offre. |
| `uv run job-agent run [--max N] [--threshold 60] [--rate 5] [--headless]` | Traite toutes les offres `pending`, en respectant le rate. |
| `uv run job-agent status [--last 20]` | Affiche un tableau coloré des dernières candidatures. |
| `uv run job-agent inbox [--hours 24]` | Check IMAP sur toutes les boîtes, classifie, met à jour la DB. |

### Exemples

```bash
# Vérifier que tout est OK
uv run job-agent doctor

# Initialiser la DB et importer les emails
uv run job-agent init

# Ajouter et traiter une offre tout de suite (navigateur visible)
uv run job-agent add "https://exemple.com/offre/12345" --auto

# Vider la file pending en mode headless (10 max, score min 70, 3/h)
uv run job-agent run --max 10 --threshold 70 --rate 3 --headless

# Voir le statut des 30 dernières candidatures
uv run job-agent status --last 30

# Faire un check des mails reçus dans les 48 dernières heures
uv run job-agent inbox --hours 48
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  CLI (typer)  -  init | add | run | status | inbox | doctor │
└─────────────┬───────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────┐
│  Agent (agent.py)  -  orchestre apply_to_job() en 12 étapes │
└────┬────────────┬────────────┬────────────┬─────────────────┘
     │            │            │            │
     ▼            ▼            ▼            ▼
┌────────┐  ┌─────────┐  ┌─────────┐  ┌─────────────┐
│   db   │  │  llm    │  │ browser │  │ imap_reader │
│ sqlite │  │ Claude  │  │ patch   │  │  imap_tools │
│        │  │ Opus 4.7│  │ right + │  │  + classif  │
│        │  │ +Haiku  │  │ browser │  │   Haiku     │
│        │  │ 4.5     │  │ -use    │  │             │
└────────┘  └─────────┘  └─────────┘  └─────────────┘
     ▲                                       ▲
     │                                       │
┌────┴────────┐                       ┌──────┴────────┐
│ email_pool  │                       │ cv_adapter    │
│ rotation    │                       │ pypdf extract │
│ + sticky    │                       │ + artifacts   │
└─────────────┘                       └───────────────┘
```

**Modèles** :
- `claude-opus-4-7` : scoring + écriture de lettres + réponses Q&A formulaire (qualité prioritaire).
- `claude-haiku-4-5-20251001` : classification d'emails entrants (volume, rapide, moins cher).

**Données** :
- `data/agent.db` : SQLite (4 tables : `emails`, `applications`, `email_assignments`, `inbox_events`).
- `data/generated/app_<id>/` : lettre + copie du CV de base, **un dossier par candidature**.
- `data/logs/agent.log.YYYY-MM-DD` : rotation quotidienne, 14 backups.

---

## FAQ

### Et si je suis bloqué par un CAPTCHA ?
L'agent **détecte** les CAPTCHA classiques (reCAPTCHA, hCaptcha, Cloudflare). Il **met en pause** le navigateur, affiche une alerte dans la console et attend que tu appuies sur **Entrée** après avoir résolu le challenge manuellement. Aucun service tiers de contournement (type 2Captcha) n'est utilisé.

### Et si Gmail me demande une vérification ?
Tu dois utiliser un **mot de passe d'application**, pas ton mot de passe principal. Cela nécessite que la 2FA soit activée. Lien : https://myaccount.google.com/apppasswords. Si la connexion IMAP échoue, `job-agent init` marquera l'email comme `invalid` automatiquement.

### Pourquoi mon offre est en status `failed` ?
Plusieurs causes possibles, lisibles dans la colonne `notes` (`uv run job-agent status`) :
- `email pool exhausted for company` : toutes tes adresses ont déjà postulé chez cette entreprise.
- `success_not_detected` : la soumission a peut-être abouti mais aucun signal de succès (URL, message visible, rapport de l'agent) n'a été détecté. Vérifie manuellement.
- `captcha` : un CAPTCHA a bloqué la soumission.
- `goto failed`, `browser-use error` : erreur réseau ou DOM inattendu.

Aucun retry automatique : à toi de relancer ou non, après inspection.

### Comment fonctionne la rotation des emails ?
- À chaque company (entreprise), un email du pool est **assigné** la première fois (et reste collé : on rappellera toujours le même email pour cette company).
- Pour une nouvelle company, on prend l'email **actif** avec le `last_used_at` le plus ancien (les nouveaux passent en premier).
- Si **toutes** les adresses ont déjà postulé chez une company donnée, l'agent **refuse** la candidature (`status=failed`).
- `last_used_at` est mis à jour **après une soumission réussie** uniquement.

### Comment fonctionne la confirmation par email ?
Après une soumission réussie, un thread daemon polle l'IMAP **toutes les 30 secondes pendant 5 minutes**. S'il trouve un mail contenant un lien de confirmation (mots-clés `confirm`, `verify`, `activate`, `confirmer`, `vérifier`, `valider`), il visite l'URL et clique sur le bouton de validation. Le status passe à `confirmed`.

Si rien dans les 5 minutes : le status reste `submitted`. Un `job-agent inbox` ultérieur peut rattraper la confirmation tardive.

### Combien de candidatures par heure ?
**5 par défaut** (`--rate 5`). Tu peux l'ajuster, mais reste raisonnable : les sites détectent les comportements suspects.

### Et si je veux interrompre une session en cours ?
`Ctrl-C` interrompt proprement. Les candidatures en cours de traitement sont sauvegardées dans la DB avec leur état au moment de l'interruption.

### L'agent peut-il modifier mon CV par offre ?
Non. Cet agent **ne modifie pas le PDF** du CV (génération PDF par offre = hors scope). Il **utilise le texte du CV pour personnaliser la lettre** et le scoring. Une copie du CV de base est archivée dans `data/generated/app_<id>/cv.pdf` à chaque candidature.

### Comment lancer les tests ?
```bash
uv run pytest                                  # tous les tests
uv run pytest --cov --cov-report=term-missing  # avec couverture
uv run ruff check src tests                    # lint
```

---

## Limites connues

- **Sites JS lourds** (LinkedIn, Indeed avec login obligatoire) : la soumission peut nécessiter une intervention manuelle au premier passage (CAPTCHA, sélection de profil, etc.).
- **Anti-bots agressifs** : certains ATS (Workday, Greenhouse custom) détectent les comportements automatisés malgré patchright. Statut typique : `success_not_detected` ou `captcha`.
- **Pas de service de contournement de CAPTCHA** : pause humaine uniquement.
- **Pas de génération de CV adapté par offre** : seule la lettre est personnalisée.
- **IMAP poll** : non poussé en push (XOAUTH2). Polling 30s suffit en pratique.

---

## Licence

MIT. Voir `LICENSE`.

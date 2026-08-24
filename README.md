# strava-openapi-mcp

Serveur MCP local en Python qui agit comme proxy générique entre un client MCP (notamment OpenCode) et l’API REST Strava. Les tools ne sont pas codés endpoint par endpoint : ils sont générés au démarrage à partir de la spécification officielle Swagger 2.0 de Strava.

Le dépôt contient une copie de la spec et de ses documents de schémas référencés. Le démarrage ne nécessite donc pas Internet pour construire la liste des tools. La commande `update-spec` permet de rafraîchir la copie utilisateur après validation.

## Architecture

`openapi.py` charge/valide le Swagger, résout les références locales et normalise les opérations. `tools.py` transforme chaque opération en tool MCP avec un JSON Schema généré. `client.py` construit les URL, paramètres, bodies JSON et formulaires multipart sans connaître les endpoints Strava. `auth.py` gère le flux OAuth local et le renouvellement des tokens. `server.py` expose le tout sur MCP stdio et `cli.py` fournit les commandes de maintenance.

La spec actuellement publiée par Strava est Swagger 2.0, `info.version` `3.0.0`. Le bundle est volontairement traité comme une donnée remplaçable : si un nouvel endpoint apparaît dans la spec, il est découvert automatiquement.

## Pré-requis et installation locale

Python 3.12+ et [uv](https://docs.astral.sh/uv/) sont recommandés.

```bash
git clone https://github.com/<USER>/<REPO>.git
cd <REPO>
uv sync
uv run strava-mcp list-tools
```

Le serveur MCP lui-même se lance ainsi et reste actif sur stdin/stdout MCP :

```bash
uv run strava-mcp
```

Les logs applicatifs vont sur stderr. Aucun log de diagnostic ne doit être écrit sur stdout pendant le transport stdio.

## Créer l’application Strava

1. Ouvrir `https://www.strava.com/settings/api`.
2. Créer une application et relever son **Client ID** et son **Client Secret**.
3. `localhost` et `127.0.0.1` sont acceptés comme domaines de callback par Strava. Le callback utilisé par défaut est `http://127.0.0.1:8765/callback`.

Les credentials peuvent être fournis par environnement :

```bash
export STRAVA_CLIENT_ID="..."
export STRAVA_CLIENT_SECRET="..."
```

Ou dans `~/.config/strava-mcp/credentials.json` avec les permissions `0600` :

```json
{
  "client_id": "...",
  "client_secret": "..."
}
```

L’environnement est prioritaire. Le secret n’est jamais affiché ni écrit dans les logs.

## OAuth

Lancer une fois :

```bash
strava-mcp auth
```

Le navigateur ouvre la page Strava, puis le callback local échange le code contre `access_token`, `refresh_token`, `expires_at` et les scopes effectivement accordés. Les tokens sont stockés dans `~/.config/strava-mcp/tokens.json` avec permissions `0600`. Le serveur renouvelle automatiquement l’access token expiré et persiste le refresh token éventuellement rotatif renvoyé par Strava.

Par défaut, tous les scopes déclarés par la spec sont demandés. Pour demander un sous-ensemble :

```bash
export STRAVA_OAUTH_SCOPES="activity:read,activity:write"
```

Les descriptions officielles sont analysées pour déduire les scopes explicites. Les endpoints de lecture qui acceptent `activity:read` ou `activity:read_all` sont représentés comme une alternative. Un scope conditionnel (par exemple `activity:read_all` pour une activité privée) est indiqué au LLM et l’erreur Strava reste visible.

## Configuration

Variables supportées :

| Variable | Défaut |
| --- | --- |
| `STRAVA_CLIENT_ID` | aucun, ou `credentials.json` |
| `STRAVA_CLIENT_SECRET` | aucun, ou `credentials.json` |
| `STRAVA_API_BASE_URL` | `https://www.strava.com/api/v3` |
| `STRAVA_OPENAPI_URL` | `https://developers.strava.com/swagger/swagger.json` |
| `STRAVA_OPENAPI_PATH` | `~/.config/strava-mcp/openapi.json` |
| `STRAVA_ALLOW_WRITE` | `true` |
| `STRAVA_ALLOW_DELETE` | `false` |
| `STRAVA_LOG_LEVEL` | `INFO` |
| `STRAVA_OAUTH_SCOPES` | tous les scopes Strava déclarés |
| `STRAVA_CALLBACK_HOST` / `STRAVA_CALLBACK_PORT` | `127.0.0.1` / `8765` |

Les alias `STRAVA_MCP_ALLOW_WRITE` et `STRAVA_MCP_ALLOW_DELETE` sont également acceptés. `strava-mcp show-config` affiche uniquement une vue non secrète de la configuration.

Les valeurs recommandées sont `STRAVA_ALLOW_WRITE=true` et `STRAVA_ALLOW_DELETE=false`. Les méthodes POST, PUT et PATCH ne sont pas bloquées par défaut. Les méthodes DELETE sont générées lorsque la spec les contient, mais filtrées de la liste MCP tant que `STRAVA_ALLOW_DELETE=false`.

## Premier démarrage

```bash
export STRAVA_CLIENT_ID="..."
export STRAVA_CLIENT_SECRET="..."
strava-mcp auth
strava-mcp list-tools
strava-mcp
```

La copie utilisateur de la spec est privilégiée. Si elle n’existe pas, le bundle officiel embarqué est utilisé, sans téléchargement au démarrage.

## Mise à jour de la spec

```bash
strava-mcp update-spec
```

La commande télécharge `STRAVA_OPENAPI_URL`, valide le Swagger puis télécharge les documents JSON référencés. La copie existante n’est remplacée qu’après réussite complète du téléchargement et de la validation. La version annoncée et le nombre de schémas référencés sont affichés.

Pour forcer un chemin différent :

```bash
STRAVA_OPENAPI_PATH="$HOME/.config/strava-mcp/openapi.json" strava-mcp update-spec
```

## Installation directe avec uvx depuis Git

Le `pyproject.toml` déclare le script et toutes les dépendances. Une installation Python manuelle ou un clone ne sont pas nécessaires :

```bash
uvx --from git+https://github.com/<USER>/<REPO> strava-mcp auth
uvx --from git+https://github.com/<USER>/<REPO> strava-mcp
```

Pour prendre immédiatement un nouveau commit malgré le cache uv :

```bash
uvx --refresh --from git+https://github.com/<USER>/<REPO> strava-mcp
```

## Configuration OpenCode

Ajouter le serveur dans la configuration OpenCode :

```json
{
  "mcp": {
    "strava": {
      "type": "local",
      "command": [
        "uvx",
        "--from",
        "git+https://github.com/<USER>/<REPO>",
        "strava-mcp"
      ],
      "enabled": true
    }
  }
}
```

Exporter les variables dans l’environnement qui lance OpenCode, ou utiliser `credentials.json`, plutôt que de committer les secrets dans ce fichier. Exécuter `strava-mcp auth` une fois avec le même compte local avant de démarrer OpenCode.

## Tools générés et exemples

Les noms viennent de `operationId`, normalisé en snake case, avec le préfixe HTTP seulement lorsque nécessaire pour éviter une ambiguïté. Par exemple, avec la spec actuelle :

| Endpoint | Tool généré actuel |
| --- | --- |
| `GET /athlete` | `get_logged_in_athlete` |
| `GET /athlete/activities` | `get_logged_in_athlete_activities` |
| `GET /activities/{id}` | `get_activity_by_id` |
| `PUT /activities/{id}` | `put_update_activity_by_id` |
| `GET /activities/{id}/streams` | `get_activity_streams` |
| `GET /athletes/{id}/stats` | `get_stats` |

Les paramètres du body `UpdatableActivity` sont aplatis dans le tool PUT. L’agent peut donc appeler conceptuellement :

```text
put_update_activity_by_id(id=123456789, name="Sortie longue Z2")
put_update_activity_by_id(id=123456789, description="Séance réalisée en endurance fondamentale, sensations bonnes.")
```

Autres exemples de demandes naturelles :

- « Liste mes dernières activités de course à pied » : utiliser `get_logged_in_athlete_activities`, puis filtrer les résultats retournés.
- « Lis le détail de l’activité 123 » : utiliser `get_activity_by_id(id=123)`.
- « Récupère les streams distance et heartrate de 123 » : utiliser `get_activity_streams(id=123, keys=["distance", "heartrate"], key_by_type=true)`.
- « Récupère mes statistiques » : obtenir l’athlète authentifié puis utiliser `get_stats(id=...)`.

La pagination est entièrement pilotée par les paramètres de la spec (`page`, `per_page`, `before`, `after`, `page_size`, `after_cursor`, etc.). Le serveur ne lance jamais automatiquement une longue série de pages.

## Écritures et opérations dangereuses

Les descriptions MCP indiquent `This operation modifies Strava data` pour POST/PUT/PATCH et `WARNING` pour DELETE. Si `STRAVA_ALLOW_WRITE=false`, les tools d’écriture répondent avec une erreur explicite. Si `STRAVA_ALLOW_DELETE=false`, les DELETE sont absents de `list_tools` et un appel direct est refusé.

Les erreurs HTTP conservent le statut, l’endpoint, le message Strava et les headers de limitation disponibles, par exemple :

```text
HTTP 401 Unauthorized
Endpoint: PUT /activities/{id}
Message: Invalid or expired token
```

Une réponse 204 devient un objet minimal `{ "status": "success", "http_status": 204 }`. Les réponses JSON gardent les noms de champs Strava.

## Commandes CLI

```text
strava-mcp                  # serveur MCP stdio
strava-mcp auth             # OAuth navigateur + callback localhost
strava-mcp update-spec      # mise à jour validée de la copie locale
strava-mcp show-config      # configuration non secrète
strava-mcp list-tools       # méthode, endpoint, tool et résumé
```

## Tests et développement

```bash
uv run pytest
uv run ruff check .
```

Les tests utilisent des transports HTTP mockés et ne contactent pas Strava. Le test d’intégration contre Strava n’est volontairement pas exécuté automatiquement.

## Troubleshooting

- `No Strava authorization found` : exécuter `strava-mcp auth` avec les credentials corrects.
- `OAuth scope missing` : refaire `strava-mcp auth` avec le scope demandé dans `STRAVA_OAUTH_SCOPES`.
- `Spec update aborted` : la copie locale précédente reste intacte ; vérifier le réseau ou revenir à `STRAVA_OPENAPI_PATH` non personnalisé.
- Aucun tool DELETE : c’est le comportement par défaut ; définir `STRAVA_ALLOW_DELETE=true` et redémarrer.
- Erreur MCP liée à stdout : ne pas ajouter de `print` dans le code du serveur ; les logs doivent utiliser logging, configuré sur stderr.
- Port OAuth occupé : définir `STRAVA_CALLBACK_PORT` sur un port libre et enregistrer le domaine localhost dans l’application Strava si nécessaire.

## Sécurité

Le client secret, l’access token et le refresh token ne sont jamais inclus dans les logs, descriptions MCP ou messages d’erreur. Les fichiers locaux de credentials et tokens sont ignorés par Git et écrits avec permissions `0600`. Ne jamais committer `.env`, `credentials.json` ou `tokens.json`.

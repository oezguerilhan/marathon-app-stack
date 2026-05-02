# HM-App Stack — Setup-Anleitung

Single-File HTML-App + FastAPI-Backend für Polar Auto-Sync.
Runs **VPN-only** im Heimnetz — keine öffentliche Exposition.

---

## Architektur (Recap)

```
WireGuard ──► NPM (Cloudflare-DNS-Cert)
              ├── hm.deinedomain.de        → hm-frontend  (nginx :8081)
              └── hm.deinedomain.de/api/*  → hm-backend   (FastAPI :8000)
                                              ├── täglicher Cron 03:00 UTC
                                              ├── Polling Polar AccessLink
                                              └── Volume /data persistent
```

---

## 1. Polar AccessLink registrieren (~10 Min)

1. Account anlegen auf [admin.polaraccesslink.com](https://admin.polaraccesslink.com)
2. **Create application** mit:
   - Application Name: `HM-Trainings-App`
   - Company / Organization: dein Name oder `Privat`
   - Application Website: dein GitHub-Profil oder `https://example.com`
   - Description: `Personal half-marathon training journal`
   - Privacy Policy URL: dein GitHub-Gist o.ä.
   - **Authorization Redirect URL:** `https://hm.deinedomain.de/api/polar/auth/callback`
   - Webhook URL: leer lassen (Polling-Setup, kein Push)
3. Nach dem Anlegen siehst du:
   - `Client ID`
   - `Client Secret`
   
   → diese gleich in die `.env` (Schritt 4)

---

## 2. Cloudflare DNS

A-Record anlegen:
- **Name:** `hm`
- **IPv4:** LAN-IP deines Docker-Hosts (`192.168.x.y`)
- **Proxy-Status:** **DNS only** (graue Wolke!) — orange würde Cloudflare-Proxy einschalten, das willst du für eine LAN-IP nicht
- TTL: Auto

API-Token für DNS-Challenge (für NPM):
- Cloudflare → My Profile → API Tokens → **Create Token**
- Template: **Edit zone DNS**
- Zone Resources: `Include — Specific zone — deine-domain.de`
- Token kopieren (nur einmal sichtbar)

---

## 3. Stack auf Docker-Host platzieren

Verzeichnisstruktur (z.B. unter `/opt/hm-app/`):

```
hm-app/
├── docker-compose.yml
├── hm_app.html             ← die HM-App
├── .env                    ← aus .env.example kopieren
├── data/                   ← persistente Volumes (token.json, polar_runs.json)
└── backend/
    ├── Dockerfile
    ├── requirements.txt
    └── app.py
```

```bash
# Auf dem Docker-Host:
sudo mkdir -p /opt/hm-app
sudo chown $USER:$USER /opt/hm-app
cd /opt/hm-app

# Inhalt aus diesem Repo nach /opt/hm-app/ kopieren (scp/rsync/git/etc)
# z.B. wenn lokal auf dem Mac:
#   scp -r ./hm-app-stack/* user@docker-host:/opt/hm-app/

cp .env.example .env
nano .env   # Polar-Credentials einfügen
```

---

## 4. `.env` ausfüllen

```bash
POLAR_CLIENT_ID=abc123-...                      # aus Polar AccessLink
POLAR_CLIENT_SECRET=xyz...                      # aus Polar AccessLink
POLAR_REDIRECT_URI=https://hm.deinedomain.de/api/polar/auth/callback
SYNC_HOUR_UTC=3                                 # 04:00 MEZ / 05:00 MESZ
```

---

## 5. Stack starten

**Via Portainer:**
- Stacks → Add stack → Name `hm-app`
- Build method: **Upload** oder **Repository** oder Web-Editor (Inhalt der `docker-compose.yml` einfügen)
- Environment variables: aus `.env` einfügen
- Deploy

**Via CLI auf dem Host:**
```bash
cd /opt/hm-app
docker compose up -d --build
docker compose logs -f hm-backend
```

Verifikation:
```bash
curl http://localhost:8081       # Frontend antwortet HTML
curl http://localhost:8000/api/health
# {"ok":true, "authorised":false, "runs_cached":0, "last_sync":null}
```

---

## 6. NPM Proxy Host

NPM → Hosts → Proxy Hosts → **Add Proxy Host**

**Details:**
| Feld | Wert |
|---|---|
| Domain Names | `hm.deinedomain.de` |
| Scheme | `http` |
| Forward Hostname/IP | LAN-IP des Docker-Hosts |
| Forward Port | `8081` |
| Block Common Exploits | ☑ |
| Websockets Support | ☑ |

**SSL:**
| Feld | Wert |
|---|---|
| SSL Certificate | Request a new SSL Certificate |
| Force SSL | ☑ |
| HTTP/2 Support | ☑ |
| HSTS Enabled | ☑ |
| **Use a DNS Challenge** | ☑ |
| DNS Provider | Cloudflare |
| Credentials File Content | `dns_cloudflare_api_token=DEIN_TOKEN` |
| Email | deine E-Mail |
| Agree to ToS | ☑ |

**Advanced** (für `/api/`-Routing zum Backend):
```nginx
location /api/ {
    proxy_pass http://DOCKER_HOST_LAN_IP:8000/api/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```
(Pfad wird **nicht** gestrippt: `/api/health` bleibt `/api/health`)

→ **Save**

---

## 7. OAuth-Flow durchführen (einmalig)

1. WireGuard verbinden
2. Im Browser: `https://hm.deinedomain.de/api/polar/auth/start`
3. Polar-Login → App autorisieren → automatischer Redirect
4. Du siehst eine JSON-Response `{"ok":true, "message":"Authorisierung erfolgreich..."}`
5. Token liegt jetzt in `/opt/hm-app/data/token.json`

Verifikation:
```bash
curl https://hm.deinedomain.de/api/health
# {"ok":true, "authorised":true, "runs_cached":0, "last_sync":null}
```

---

## 8. Erster Sync

Im Browser auf `https://hm.deinedomain.de` → Logbook-Tab → **„↻ Sync jetzt"**.

Oder direkt:
```bash
curl -X POST https://hm.deinedomain.de/api/sync-now
```

Der Backend pulled alle bisher unsynchronisierten Polar-Activities. Polar AccessLink liefert nur Activities ab Registrierung — für **historische Läufe** den TCX-Bulk-Import in derselben App nutzen (Logbook-Tab → „Polar TCX Import").

---

## Verifikation (Ende-zu-Ende)

| Test | erwartet |
|---|---|
| Frontend öffnen | App lädt, im Logbook-Tab steht „Letzter Sync: noch nie" |
| `/api/health` | `authorised: true` nach OAuth |
| TCX-Datei aus Polar Flow hochladen | Eintrag erscheint in Logbook |
| Selbe TCX-Datei nochmal hochladen | „1 Duplikat übersprungen" |
| Kurzlauf mit M3, danach Sync zu Polar Flow auf iPhone, dann „Sync jetzt" | Eintrag mit `note: "Polar API ..."` und `polarId` erscheint |
| Tag wartet (Cron) | Container-Log zeigt nächsten Morgen erfolgreichen Pull |
| Polar-Run im Logbook löschen, „Sync jetzt" | Lauf kommt **nicht** zurück (deletedPolarIds greift) |
| Backend stoppen | App weiter benutzbar (manuelle Einträge) |

---

## Troubleshooting

**„Backend offline / nicht erreichbar"** im Logbook
→ NPM erreicht den Backend-Container nicht. Prüfen:
```bash
curl http://DOCKER_HOST_LAN_IP:8000/api/health
```
Wenn das geht, ist der NPM-Advanced-Block falsch (Pfad oder Port).

**„Polar tx create failed: 403"**
→ Token abgelaufen oder Scope falsch. OAuth nochmal: `/api/polar/auth/start`

**`ModuleNotFoundError` beim Container-Start**
→ Image neu bauen: `docker compose up -d --build hm-backend`

**Container kann `data/` nicht schreiben**
→ Volume-Permissions: `sudo chown -R 1000:1000 /opt/hm-app/data`

---

## Updates

App-Update (z.B. neue HM-App-Version):
```bash
cp /pfad/zur/neuen/hm_app.html /opt/hm-app/hm_app.html
docker compose restart hm-frontend
```

Backend-Update (Code-Änderung):
```bash
docker compose up -d --build hm-backend
```

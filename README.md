# Bot de știri – Pagina „Victor Vîlceanu finanțe”

Publică automat știri financiare (România, piețe internaționale, forex și macro) pe Pagina de Facebook. Rulează pe GitHub Actions: în zilele lucrătoare, la fiecare 2 ore între 08:17 și 18:17 (6 postări pe zi), iar în weekend de 2 ori pe zi. Fiecare postare conține un rezumat AI scris în română și linkul către sursă.

## Fișiere
| Fișier | Rol |
|---|---|
| `news_bot.py` | scriptul: RSS → filtrare → Gemini → Graph API |
| `feeds.json` | lista surselor RSS; poți adăuga sau scoate surse |
| `posted.json` | istoricul postărilor, ca nimic să nu apară de două ori |
| `.github/workflows/news.yml` | programarea |

## 1. Tokenul Paginii (îl generezi tu, nu îl trimiți nimănui)
1. Deschide **developers.facebook.com/tools/explorer**.
2. Dreapta sus: **Meta App** → `Stiri Victor Vilceanu finante`.
3. **User or Page** → *Get User Access Token*. Bifează `pages_show_list`, `pages_manage_posts`, `pages_read_engagement` → **Generate Access Token** → alege Pagina „Victor Vîlceanu finanțe”.
4. Apasă pe ⓘ de lângă token → **Open in Access Token Tool** → **Extend Access Token**. Copiază tokenul lung (valabil 60 de zile).
5. Înapoi în Explorer, lipește tokenul lung în câmpul *Access Token* și rulează `GET me/accounts`. Copiază câmpul `access_token` din dreptul Paginii. Acesta este **Page Access Token** și, obținut astfel, nu expiră.

## 2. Cheia Gemini
**aistudio.google.com/apikey** → *Create API key*.

## 3. GitHub
1. Creează un repository **privat**, de exemplu `fb-news-bot`, și încarcă toate fișierele (inclusiv folderul `.github`).
2. **Settings → Secrets and variables → Actions → New repository secret**:
   - `FB_PAGE_TOKEN` = tokenul de la pasul 1
   - `GEMINI_API_KEY` = cheia de la pasul 2
3. **Actions** → *Stiri pe Pagina Facebook* → **Run workflow**, cu `dry_run = 1`. În log vezi postarea fără să fie publicată.
4. Dacă textul arată bine, rulează încă o dată cu `dry_run = 0`. Postarea apare pe Pagină, iar de aici rularea programată merge singură.

## Ajustări rapide
- Mai multe postări pe rulare: în `news.yml`, la `env`, adaugă `POSTS_PER_RUN: "2"`.
- Alt orar: modifici liniile `cron`. Orele sunt în UTC; România este UTC+2 iarna și UTC+3 vara.
- O sursă care nu mai merge e doar sărită și apare în log ca „[sursă ignorată]”.

## Notă
Fiecare postare se încheie cu mențiunea „Informație cu caracter general, nu reprezintă recomandare de investiții.”. Scriptul îi cere lui Gemini să nu facă recomandări și să nu inventeze cifre, dar postările nu sunt verificate de un om înainte de publicare.

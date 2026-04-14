# Doctor Collector

License: MIT
Python 3.11+
Docker

A CLI tool that collects therapist contact information from [therapie.de](https://www.therapie.de) and optionally contacts them via email. Talks directly to therapie.de's server-rendered HTML — no browser automation or Selenium required.

## Table of Contents

- [Quick Start](#quick-start)
- [Usage](#usage)
- [Configuration](#configuration)
- [Deployment](#deployment)
- [Development](#development)

## Quick Start

### Install

Requires Python 3.11+.

```bash
git clone https://github.com/kequach/Doctor-collector.git
cd Doctor-collector
python -m pip install -e .
```

### Run

The tool has two modes that can be used independently or together:

```bash
# Collect therapist data only (saves to therapists.csv)
python -m doctor_collector --collect

# Contact therapists only (uses previously collected data)
python -m doctor_collector --contact

# Collect and contact in one run
python -m doctor_collector --collect --contact
```

Before running, edit `config.yaml` to set your postal code and (for `--contact`) your SMTP credentials.

## Usage

### Mode 1: Collect (`--collect`)

Scrapes therapist profiles from therapie.de based on your search parameters, applies filters, and saves matching therapists to `therapists.csv`.

```bash
python -m doctor_collector --collect
```

Output:

```
============================================================
  Doctor Collector — 5 therapist(s)
============================================================

  1. Dr. Maria Schmidt
     Psychologische Psychotherapeutin
     Email:   m.schmidt@example.de
     Website: https://www.praxis-schmidt.de
     Profile: https://www.therapie.de/profil/schmidt/

  2. Thomas Müller
     Psychologischer Psychotherapeut
     Email:   t.mueller@example.de
     Profile: https://www.therapie.de/profil/mueller/

  ...

  Scraped 42 profiles, 5 matched your filters.
  Results saved to therapists.csv
```

### Mode 2: Contact (`--contact`)

Sends your email template to all therapists in `therapists.csv` who haven't been contacted yet. Tracks contacted emails in `.contacted_therapists.json` so you never email anyone twice.

```bash
python -m doctor_collector --contact
```

### Both together

Collect fresh data and immediately contact new therapists:

```bash
python -m doctor_collector --collect --contact
```

### Custom config path

```bash
python -m doctor_collector --collect --config /path/to/my-config.yaml
```

## Configuration

All settings live in `config.yaml`. You can also set them via environment variables.

### Search parameters

```yaml
therapie:
  post_code: "10115"           # German postal code (5 km radius)
  therapy_form: 1              # 1=Einzeltherapie, 2=Gruppentherapie, 3=Paar-/Familientherapie
  therapy_type: 2              # 1=Analytische, 2=Verhaltenstherapie, 3=Tiefenpsych., 4=Systemische
  start_page: 1                # page to start from (for resuming)
  max_pages: 100               # max listing pages to crawl
  request_delay_seconds: 1.0   # delay between requests (rate limiting)
```

### Filters

```yaml
filters:
  exclude_types:
    - "Heil"      # exclude Heilpraktiker
    - "Kinder"    # exclude child therapists
    - "Privat"    # exclude private-only therapists
```

Therapists whose type description contains any keyword (case-insensitive) are excluded. Only therapists with an email address are included.

### Contact settings

Used when running with `--contact`. Gmail requires an [App Password](https://support.google.com/accounts/answer/185833) (enable 2-Step Verification first).

```yaml
contact:
  subject: "Erstgespräch Anfrage"
  body: |
    Sehr geehrte Damen und Herren,
    ...
  smtp_host: "smtp.gmail.com"
  smtp_port: 465
  use_tls: true
  smtp_user: "you@gmail.com"
  smtp_password: "your-app-password"
  from_address: "you@gmail.com"
```

**Other email providers:**

| Provider | smtp_host | smtp_port | Notes |
|----------|-----------|-----------|-------|
| Gmail | smtp.gmail.com | 465 | Requires [App Password](https://support.google.com/accounts/answer/185833) |
| Outlook / Microsoft 365 | smtp.office365.com | 587 | Use full email as smtp_user |
| Yahoo | smtp.mail.yahoo.com | 587 | Requires [App Password](https://help.yahoo.com/kb/generate-manage-third-party-passwords-sln15241.html) |

### Environment variables

Every config option can be set via environment variables:

| Env Variable | Type | Config Equivalent |
|-------------|------|-------------------|
| `THERAPIE_POST_CODE` | string | `therapie.post_code` |
| `THERAPIE_THERAPY_FORM` | int | `therapie.therapy_form` |
| `THERAPIE_THERAPY_TYPE` | int | `therapie.therapy_type` |
| `THERAPIE_START_PAGE` | int | `therapie.start_page` |
| `THERAPIE_MAX_PAGES` | int | `therapie.max_pages` |
| `THERAPIE_REQUEST_DELAY` | float | `therapie.request_delay_seconds` |
| `FILTER_EXCLUDE_TYPES` | comma-separated | `filters.exclude_types` |
| `CONTACT_SUBJECT` | string | `contact.subject` |
| `CONTACT_BODY` | string | `contact.body` |
| `CONTACT_SMTP_HOST` | string | `contact.smtp_host` |
| `CONTACT_SMTP_PORT` | int | `contact.smtp_port` |
| `CONTACT_USE_TLS` | true/false | `contact.use_tls` |
| `CONTACT_SMTP_USER` | string | `contact.smtp_user` |
| `CONTACT_SMTP_PASSWORD` | string | `contact.smtp_password` |
| `CONTACT_FROM_ADDRESS` | string | `contact.from_address` |

## Deployment

### Docker

**Build:**

```bash
docker build -t doctor-collector .
```

**Collect only:**

```bash
docker run --rm \
  -v ./config.yaml:/app/config.yaml \
  -v ./data:/app/data \
  -e STATE_FILE=/app/data/.contacted_therapists.json \
  -e CSV_FILE=/app/data/therapists.csv \
  doctor-collector \
  python -m doctor_collector --collect
```

**Contact only:**

```bash
docker run --rm \
  -v ./config.yaml:/app/config.yaml \
  -v ./data:/app/data \
  -e STATE_FILE=/app/data/.contacted_therapists.json \
  -e CSV_FILE=/app/data/therapists.csv \
  doctor-collector \
  python -m doctor_collector --contact
```

**Collect and contact:**

```bash
docker run --rm \
  -v ./config.yaml:/app/config.yaml \
  -v ./data:/app/data \
  -e STATE_FILE=/app/data/.contacted_therapists.json \
  -e CSV_FILE=/app/data/therapists.csv \
  doctor-collector \
  python -m doctor_collector --collect --contact
```

**Using Docker Compose:**

```bash
# Default (collect only, as defined in docker-compose.yml):
docker compose run --rm doctor-collector

# Contact only:
docker compose run --rm doctor-collector python -m doctor_collector --contact

# Both:
docker compose run --rm doctor-collector python -m doctor_collector --collect --contact
```

### Docker with env vars only

```bash
docker run --rm \
  -v ./data:/app/data \
  -e STATE_FILE=/app/data/.contacted_therapists.json \
  -e CSV_FILE=/app/data/therapists.csv \
  -e THERAPIE_POST_CODE=10115 \
  -e THERAPIE_THERAPY_FORM=1 \
  -e THERAPIE_THERAPY_TYPE=2 \
  -e FILTER_EXCLUDE_TYPES=Heil,Kinder,Privat \
  doctor-collector \
  python -m doctor_collector --collect
```

### Data files

| File | Purpose |
|------|---------|
| `therapists.csv` | Collected therapist data (name, email, type, website, profile URL) |
| `.contacted_therapists.json` | Tracks which emails have been sent to prevent duplicates |

Both are created automatically. The state file ensures you never contact the same therapist twice, even across multiple runs.

## Development

```bash
pip install -e ".[dev]"
python -m pytest tests/ -v
python -m ruff check src/ tests/
```

### How it works

The collector fetches therapist listings from [therapie.de](https://www.therapie.de) using pure HTTP requests (httpx). Email addresses are embedded in the HTML source as obfuscated `data-contact-email` attributes on the contact button — no browser automation needed. The collector decodes them with a simple character-shift cipher and extracts all profile data from the server-rendered HTML.

### Project structure

```
src/doctor_collector/
├── __main__.py              # CLI entry-point (--collect / --contact)
├── config.py                # YAML + env var config loading
├── clients/therapie.py      # therapie.de HTTP client (pure httpx)
├── models/therapist.py      # Pydantic models
├── services/
│   ├── collector.py         # Scraping, filtering, CSV + state management
│   └── contactor.py         # SMTP email sending to therapists
└── notifications/
    └── console.py           # CLI output formatting
```

## License

MIT

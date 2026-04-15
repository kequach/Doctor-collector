# Doctor Collector

Automatically find therapists on [therapie.de](https://www.therapie.de) and send them a request for an initial consultation (Erstgespräch) via email.

## Quick Start

### 1. Install Python

Download and install Python 3.11 or newer from the official website:

**[Download Python](https://www.python.org/downloads/)**

During installation on Windows, make sure to check **"Add Python to PATH"**.

### 2. Download this project

[Download as ZIP](https://github.com/kequach/Doctor-collector/archive/refs/heads/main.zip) and extract it, or use Git:

```
git clone https://github.com/kequach/Doctor-collector.git
```

### 3. Install dependencies

Open a terminal in the project folder and run:

```
pip install -e .
```

### 4. Configure

Open `config.yaml` in any text editor and fill in:

- **Your postal code** (the tool searches within a 5 km radius)
- **Your email credentials** (only needed if you want to send emails — see [Contact settings](#contact-settings) below)

### 5. Run

```
python -m doctor_collector --collect
```

This searches therapie.de, filters the results, and saves everything to `therapists.csv`. You can open this file in Excel.

## Usage

| Command | What it does |
|---------|-------------|
| `python -m doctor_collector --collect` | Find therapists and save to `therapists.csv` |
| `python -m doctor_collector --contact` | Send emails to therapists in `therapists.csv` |
| `python -m doctor_collector --collect --contact` | Find therapists and email them in one go |

A typical workflow:

1. Run with `--collect` first to review the results in `therapists.csv`
2. Once you're happy with the list, run with `--contact` to send emails
3. The tool remembers who you already contacted — running again only emails new therapists

## Configuration

All settings are in `config.yaml`. Open it in any text editor.

### Search settings

```yaml
therapie:
  post_code: "10115"         # your postal code (5 km search radius)
  therapy_form: 1            # 1 = Einzeltherapie, 2 = Gruppentherapie, 3 = Paar-/Familientherapie
  therapy_type: 2            # 1 = Analytische, 2 = Verhaltenstherapie, 3 = Tiefenpsychologisch, 4 = Systemische
  max_pages: 100             # how many pages of results to go through
```

### Filters

```yaml
filters:
  exclude_types:
    - "Heil"       # excludes Heilpraktiker
    - "Kinder"     # excludes child/youth therapists
    - "Privat"     # excludes private-only therapists
```

Add or remove keywords to control which therapists are excluded. Only therapists with an email address are included.

### Contact settings

Only needed when running with `--contact`. If you use Gmail, you need an **App Password** instead of your regular password:

1. Go to [myaccount.google.com](https://myaccount.google.com/) > **Security** > enable **2-Step Verification**
2. Go to [App Passwords](https://myaccount.google.com/apppasswords), create one for "Mail"
3. Copy the 16-character password into `config.yaml`:

```yaml
contact:
  subject: "Erstgespräch Anfrage"
  body: |
    Sehr geehrte Damen und Herren,

    Ich möchte ein Erstgespräch bei Ihnen anfragen.
    ...

  smtp_host: "smtp.gmail.com"
  smtp_port: 465
  use_tls: true
  smtp_user: "you@gmail.com"
  smtp_password: "your-16-char-app-password"
  from_address: "you@gmail.com"
```

<details>
<summary>Using Outlook or Yahoo instead of Gmail?</summary>

| Provider | smtp_host | smtp_port |
|----------|-----------|-----------|
| Outlook / Microsoft 365 | smtp.office365.com | 587 |
| Yahoo | smtp.mail.yahoo.com | 587 |

Both also require app passwords. See your provider's help pages for details.

</details>

## Docker

Build the image once:

```
docker build -t doctor-collector .
```

All examples below mount a `./data` folder so that output files are saved on your machine (not lost when the container stops). Fill in your values where indicated.

**Step 1 — Collect therapists:**

```
docker run --rm -v ./config.yaml:/app/config.yaml -v ./data:/app/data \
  -e CSV_FILE=/app/data/therapists.csv \
  -e THERAPIE_POST_CODE=10115 \
  doctor-collector python -m doctor_collector --collect
```

**Step 2 — Review the results:**

The collected data is saved to `./data/therapists.csv`. Open it in Excel, Google Sheets, or any text editor to review before contacting.

**Step 3 — Contact therapists:**

```
docker run --rm -v ./config.yaml:/app/config.yaml -v ./data:/app/data \
  -e CSV_FILE=/app/data/therapists.csv \
  -e STATE_FILE=/app/data/.contacted_therapists.json \
  -e CONTACT_SMTP_USER=you@gmail.com \
  -e CONTACT_SMTP_PASSWORD=your-16-char-app-password \
  -e CONTACT_FROM_ADDRESS=you@gmail.com \
  doctor-collector python -m doctor_collector --contact
```

You can also combine both steps into a single run without a config file — just fill in your values:

```
docker run --rm -v ./data:/app/data \
  -e STATE_FILE=/app/data/.contacted_therapists.json \
  -e CSV_FILE=/app/data/therapists.csv \
  -e THERAPIE_POST_CODE=10115 \
  -e THERAPIE_THERAPY_FORM=1 \
  -e THERAPIE_THERAPY_TYPE=2 \
  -e CONTACT_SMTP_USER=you@gmail.com \
  -e CONTACT_SMTP_PASSWORD=your-16-char-app-password \
  -e CONTACT_FROM_ADDRESS=you@gmail.com \
  -e CONTACT_SUBJECT="Erstgespräch Anfrage" \
  -e CONTACT_BODY="Sehr geehrte Damen und Herren, ich möchte ein Erstgespräch bei Ihnen anfragen. Mit freundlichen Grüßen, Max Mustermann" \
  doctor-collector python -m doctor_collector --collect --contact
```

## License

MIT

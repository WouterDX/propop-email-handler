# Setup — Propop email handler

This guide walks you step by step through everything needed to run the app
locally, on top of what `test_gmailaccess.py` already did.

## 0. Overview of what was added

| File | What it does |
|---|---|
| `config.py` | All settings (reads `.env`), show list, prices |
| `models.py` | Data structures (Reservation, AgentResult) with validation |
| `email_parser.py` | Converts raw email (bytes) to usable text (stdlib `email`) |
| `gmail_client.py` | Gmail integration: fetch messages/threads, labels, draft replies |
| `reservation_list.py` | Interface to the reservation list + local JSON test version |
| `ai_agent.py` | Calls OpenRouter to classify + write a reply |
| `main.py` | Connects everything: fetches emails, calls AI, creates drafts |
| `instructions_email_handler.md` | Your business instructions (unchanged) — passed literally to the AI |
| `data/company_data_example.json` | Anonymized example of structured company data (shows, URLs, gift voucher info) |

## 1. Python environment
Install pixi (follow online instructions), then:

```bash
pixi init
pixi install pixi.toml
pixi shell
```
## 2. Gmail access (setup credentials.json)

The following steps generate a file `credentials.json` that allows the python app to access a specific gmail inbox, to read emails, apply gmail labels and create draft replies (an optional extension to also send emails will be activated at a later stage).

### 2.1. Create a google cloud project in the organisation with the email you want to access.
Go to http://console.cloud.google.com.

Sign in with the Google account whose Gmail you want the program to read.

Then:

At the top of the page, click the project selector.
Click New Project.
Give it a name, for example:
Booking Email Parser
For parent resource, select your organisation. If not available, refresh the page first.
Click Create.
Select the new project.

You don't need to attach a billing account for what we're doing.

### 2.2. Enable gmail api
With your new project selected:

Open APIs & Services → Library.
Search for Gmail API.
Open Gmail API.
Click Enable.

### 2.3. Setup oauth credentials
Now go to:

APIs & Services → Credentials

You'll probably first be asked to configure the OAuth consent screen.

Select "User Data" when toggled for that.

If Google asks what type of app/user:

Choose External if that's the option presented.
Enter something simple such as:
App name: Booking Email Parser
User support email: your Gmail address
Developer contact: your Gmail address

For Scopes, don't add anything manually yet.

For Test users, add the Gmail account you're going to use.

Then save.

### 2.4. Create desktop app
You probably get a hint that you should configure credentials, click it or go to API&Services/Credentials.
Click:

Create Credentials → OAuth client ID

For application type choose:

Desktop app

Name it:

Booking Email Parser

Click Create.

Google will give you a client ID/secret.

Click Download JSON.

Save the downloaded file in your project as credentials.json. Keep it secret so never push to your repo.

### 2.5. Run the test program for the first time.
When running python src/test_gmailaccess.py, the first time you'll be redirected to your browser. The browser must open in a window where a user of your organisation is logged in. If this is not correct, you can copy-paste the url from your console log in a browser that does have this access.

After that, you're all set.

## 3. OpenRouter API key (the AI agent)

OpenRouter will allow to call any AI agent, free or paid.

1. Create a free account at **https://openrouter.ai/**.
2. Go to **https://openrouter.ai/keys** → **Create Key** → give it a name
   like "propop-email-handler-ai-agent" → copy the key (starts with `sk-or-v1-...`).
3. In case of a paid model, add a fixed amount of credits via **https://openrouter.ai/credits**
   (our initial setup will use a free model, you can modify this afterwards). There's a max of 50 requests per day for free on openrouter.
4. Copy `.env.example` to `.env` and paste your key there:
   ```bash
   cp .env.example .env
   ```
   Open `.env` in a text editor and fill in `OPENROUTER_API_KEY`.

### Which model?

By default, `.env.example` uses https://openrouter.ai/openrouter/free. 
This selects the best possible options available for free.
If this doesn't work well for your use case, it's recommended to ask a chatbot for better options that fit your budget.
Ask the chatbot to choose from models available at from the model choices available at **https://openrouter.ai/models**.
To modify your model, just change `OPENROUTER_MODEL` in `.env`, no code required.

Track usage at https://openrouter.ai/activity.

## 4. Company data JSON (required)

Business-specific data is stored at two places:
-  `data/instructions_email_handler.md`: instructions that can be read by a human or ai
-  `data/company_data.json`: data for the company in a clear structure, that can be read by computer software.

The easiest is to read the first file with instructions, and modify it for your purposes (this can be done in any language you choose).
Once you are done, create the second file as follows:

1. Copy the example file `data/company_data_example.json` to `data/company_data.json`:   
2. Ask an AI chatbot like chatgpt to modify `data/company_data.json` using `data/instructions_email_handler.md`. This is an easy job for an AI!
3. Compare both files to see if the new output is correct.
4. Optional: if you store this file elsewhere, set `COMPANY_DATA_FILE` in `.env`.

## 5. First test run (recommended: dry-run)

Start with a "dry run": the app reads your mailbox and shows what it WOULD do,
without changing anything in Gmail or in the reservation list.

```bash
python src/main.py --dry-run --max 3
```

On first run, a browser window opens so you can sign in to your
Google account and grant permission (just like with `test_gmailaccess.py`).

Review the JSON output: is the classification correct? Is the proposed reply
appropriate? If needed, adjust `instructions_email_handler.md` (the AI reads
this file again on every run) and try again.

## 6. Run live — with drafts, not auto-send

Once you trust the dry run:

```bash
python src/main.py
```

The app now actually creates **draft replies** in Gmail and labels processed
emails with `AI_Processed` (and `AI_Needs_Review` for emails that should be
reviewed manually — for example custom requests or cases where the AI is not
confident enough). **Nothing is sent automatically yet.**
A staff member reviews drafts in Gmail and sends them manually.

Only after you have confidence for a while should you set `AUTO_SEND=true`
in `.env` so replies are sent immediately. Do this only after several weeks
of reviewing drafts.

Optional Gmail search scope in `.env`:
- `INCOMING_MAIL_FOLDERS` controls where the app searches for incoming mails.
- You can use any Gmail query syntax here (for example labels, categories,
  inclusions/exclusions such as `in:anywhere -in:spam -in:trash`).

Optional `main.py` default arguments in `.env`:
- `MAIN_DEFAULT_DRY_RUN=true|false`
- `MAIN_DEFAULT_MAX_THREADS=<int>` (empty/unset means no limit)
- `MAIN_DEFAULT_DROP_LAST_ORG_REPLY=true|false`
- `MAIN_DEFAULT_RESERVATION_LIST_STUB=true|false`

These values only set defaults when corresponding CLI flags are omitted.
Any explicit CLI flags (for example `--dry-run`, `--max`,
`--drop-last-org-reply`, `--reservation-list-stub`) still override them.

## 7. Run repeatedly

It's recommended to run this script manually once every day for incoming emails.
If you do want more automation, you'll have to search information on crontab (mac/linux) or integration tools like n8n. 

## 8. Reservation list — currently a local test version

`reservation_list.py` currently contains a simple, **local** implementation
(`JsonFileReservationList`) that writes reservations to
`data/reservations.json`. This is a placeholder so the full email flow can
already be tested end to end.

To connect this to the real reservation system on the website:
1. Write a new class in `reservation_list.py` that inherits from
   `ReservationList`, with the five methods `search`, `get`, `create`,
   `update`, `cancel` implemented against the website's real database/API.
2. In `main.py`, replace the line
   `reservation_list = reservation_list_module.JsonFileReservationList()`
   with your new class.

Nothing else in the app needs to change for this.

## 9. Security & privacy — short overview

- `.env`, `credentials.json`, and `token.json` contain secret keys —
  **never** commit them to git (they are in `.gitignore`).
- By default, the app sends nothing automatically (see step 5).
- The AI agent sees email content via OpenRouter (and therefore via the
  underlying model provider, e.g. Anthropic or OpenAI, depending on your
  selected model). If needed, review the privacy policy of OpenRouter and
  the selected model provider when handling personal data of children/families.

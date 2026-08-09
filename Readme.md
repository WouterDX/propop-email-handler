# Propop email handler

The app in this repo handles email for a small theater, according to rules specified (in Dutch) in `instructions_email_handler.md`: 
it mainly handles reservations but can also deal with custom requests, and can deal both with email and structured reservations coming from website forms.

# Initial setup
To run this repo, follow [`SETUP.md`](SETUP.md).

## 1. Create a google cloud project in the organisation with the email you want to access.
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

## 2. Enable gmail api
With your new project selected:

Open APIs & Services → Library.
Search for Gmail API.
Open Gmail API.
Click Enable.

## 3. Setup oauth credentials
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

## 4. Create desktop app
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

## 5. Run the test program for the first time.
When running python src/test_gmailaccess.py, the first time you'll be redirected to your browser. The browser must open in a window where a user of your organisation is logged in. If this is not correct, you can copy-paste the url from your console log in a browser that does have this access.

After that, you're all set.

# How it works.
```
Gmail (new email)
   │
   ▼
email_parser.py   -- standard email parsing (stdlib): text, sender, thread info
   │
   ▼
gmail_client.py   -- fetches full thread history (up to 10 previous messages)
   │
   ▼
reservatielijst.py -- looks up existing reservations for this email address
   │
   ▼
ai_agent.py       -- 1 call to OpenRouter: classifies, extracts
   │                  data, writes a Dutch reply
   ▼
main.py           -- executes reservation-list action (if needed),
                      creates a draft reply in Gmail, labels the email
```

Everything runs **safely** by default: no email is sent automatically;
only drafts are created for staff review (see
`AUTO_SEND` in `SETUP.md`).

# Quickstart
```bash
pixi init
pixi install pixi.toml
pixi shell
cp .env.example .env   # fill the required info in .env, see SETUP.d
python main.py --dry-run --max 3
```
import os.path
import base64
from email import message_from_bytes

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


# For this first test, the program can ONLY read Gmail.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def get_gmail_service():
    creds = None

    # Google saves your authorization here after the first login.
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file(
            "token.json", SCOPES
        )

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                "credentials.json", SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open("token.json", "w") as token:
            token.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def get_header(headers, name):
    for header in headers:
        if header["name"].lower() == name.lower():
            return header["value"]
    return ""


def main():
    service = get_gmail_service()

    # Get the 10 most recent messages.
    results = (
        service.users()
        .messages()
        .list(userId="me", maxResults=10)
        .execute()
    )

    messages = results.get("messages", [])

    print(f"\nFound {len(messages)} recent messages.\n")

    for item in messages:
        message = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=item["id"],
                format="metadata",
                metadataHeaders=["From", "To", "Subject", "Date"],
            )
            .execute()
        )

        headers = message["payload"]["headers"]

        print("-" * 70)
        print("From:   ", get_header(headers, "From"))
        print("To:     ", get_header(headers, "To"))
        print("Subject:", get_header(headers, "Subject"))
        print("Date:   ", get_header(headers, "Date"))
        print("ID:     ", message["id"])

    print("-" * 70)


if __name__ == "__main__":
    main()
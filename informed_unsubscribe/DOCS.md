# Informed Unsubscribe

Reads your USPS Informed Delivery digests and photos you take of your mail, spots junk mail with
on-device OCR, finds an opt-out address on the sender's website and drafts a removal request for
you to review and send. Nothing is sent until you press Send.

## 1. Set up a dedicated mailbox

The app reads your Informed Delivery emails from a mailbox of their own and sends requests from
it, so your main email address is never involved.

1. Create a new Gmail account and turn on 2-Step Verification.
2. Create an app password at <https://myaccount.google.com/apppasswords>.
3. In your main Gmail: Settings → Forwarding and POP/IMAP → Add a forwarding address. Gmail
   emails a confirmation code to the new account; open it there and confirm.
4. In your main Gmail, create a filter for `from:(informeddelivery.usps.com)` with the action
   "Forward it to" the new address.

Another IMAP/SMTP provider works too; change the two mail server settings below.

Senders see the dedicated mailbox's address, so give it a neutral one.

## 2. Configure the app

Fill in the Configuration tab, start the app, and turn on "Show in sidebar" on the Info tab.

| Setting                                    | What it does                                                                                                                                                                       |
| ------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Email address, Email mailbox password      | The dedicated mailbox and its app password                                                                                                                                         |
| Physical mail address                      | Quoted in each request so the sender can find the listing. Start it with the house number; that is how the app finds the addressee on a scan                                       |
| Last names of everyone who lives here      | Mail addressed to anyone else gets a "does not live at this address" request instead of an opt-out. Leave empty to turn that off                                                   |
| Last names of people who used to live here | Optional. The app looks for these names anywhere on a piece, even when OCR misreads a letter, and their mail always gets a "does not live at this address" request                 |
| California resident                        | Adds your deletion and opt-out rights under the California Consumer Privacy Act to each opt-out request                                                                            |
| Registered with DMAchoice                  | Adds a line saying the address is registered with DMAchoice. Turn on only if it is; there is no government do-not-mail list in the US                                              |
| Brave Search API key                       | Optional [Brave Search API](https://brave.com/search/api/) key. With it, sender lookups are not rate-limited. Without it the app uses DuckDuckGo, which blocks after a few searches |
| From name                                  | Optional name shown to senders beside the mailbox address. Empty uses your street with "Resident", such as "12 Oak Court Resident"                                                 |
| Send me a copy at                          | Optional address, such as your main Gmail, that gets a blind copy of every request you send. The sender never sees it                                                              |
| Reply-to address                           | Optional address for senders' replies; otherwise they go to the dedicated mailbox. The sender does see this address                                                                |
| Incoming / Outgoing mail server            | Leave as is for Gmail                                                                                                                                                              |
| Hours between runs                         | How often the app checks the mailbox (default 24), counted from the end of the last run. Ignored when "Run daily at" is set                                                        |
| Run daily at                               | Optional time of day, 24-hour `HH:MM` in Home Assistant's time zone, such as `06:00`. The app then checks once a day at that time                                                  |
| Notify me when there is mail to review     | Shows a Home Assistant notification when new mail arrives in For Review and removes it once the queue is empty (default on)                                                        |
| Days to keep scans                         | Days to keep each scan (default 90). A piece's History entry is kept; scans of mail still waiting for a decision are kept until it is settled                                      |

The first run takes in the last 5 days of digests. Later runs catch up on anything since.

## Using the app

- **For Review** lists mail the app read, with a drafted request where one is needed. Check the
  sender, the opt-out address and the wording, then press Send. "Run now" checks the mailbox
  straight away.
- **Add photo** takes in mail that never shows up in a digest: take a picture or choose one you
  already have, and it goes through the same steps as a scan. It is shrunk in your browser first,
  which also drops what a phone stores inside a photo, such as where it was taken. A photo can
  show more than the mailpiece, so unlike a scan it is only attached to a request if you tick
  "Attach your photo" on that request.
- **Requests only repeat what is printed on the mailpiece**: the addressee as the sender wrote it
  and your address. They are not signed with a name.
- **Corrections.** Changing a piece's category, sender or addressee records your answer next to
  what the app had decided. "Download your corrections" exports these as JSON. The file contains
  names and addresses read from your mail, so treat it as private.

## Privacy

Scans and photos never leave your network, and OCR runs inside the app. The only outbound
traffic is the mailbox connection, the web search for a sender's name, the visit to the sender's
site, and the requests you choose to send.

More: <https://github.com/informedunsubscribe/informed-unsubscribe-homeassistant>

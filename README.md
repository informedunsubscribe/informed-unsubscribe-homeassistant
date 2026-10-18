# Informed Unsubscribe

<img src="informed_unsubscribe/icon.png" alt="" width="96" align="right">

A Home Assistant app that reads your USPS Informed Delivery digests and photos you take of your
mail, spots marketing mail with on-device OCR, finds an opt-out address on the sender's website
and drafts a removal request for you to review and send.

[![Add the repository to your Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Finformedunsubscribe%2Finformed-unsubscribe-homeassistant)

## How it works

1. Gmail forwards the daily digest to a dedicated mailbox.
2. The app polls that mailbox over IMAP and saves each mailpiece image, following the digest's
   layout: only pieces under "Expected Today" are taken ("Expected This Week" is ignored),
   the advertiser pictures shown under a piece are skipped, and where USPS prints
   "FROM: advertiser" above a piece, that name is used as the sender and the piece counts as
   marketing unless it looks like a bill.
3. Tesseract OCR, running inside the app, reads the text on each scan. The app takes the
   sender from the return address (or a web address printed on the piece) and labels the piece
   Junk Mail, Not Junk or Unclassified, using the strongest evidence it finds, in this order:
   1. Bulk-mail postage markings ("PRSRT STD", "Nonprofit Org", "Or Current Resident"):
      marketing.
   2. Bill wording ("invoice", "amount due", "statement enclosed"): not junk, even from a
      sender you marked as marketing.
   3. Your own earlier label for that sender.
   4. USPS listing the piece as an advertiser's campaign: marketing.
   5. Offer and announcement wording ("free estimate", "20% off", "just sold", "grand opening").
   6. First-class or official markings with no offer wording: not junk.
4. If you list your household's last names, the app also reads the addressee above your street
   address. Mail for anyone else, marketing or not, gets a "does not live at this address"
   request instead of an opt-out.

   You can also list the last names of people who used to live at your address. The app looks
   for those names anywhere on the piece, so it finds the addressee even when the name is
   printed off to one side or OCR misreads a letter. Their mail always gets a "does not live at
   this address" request, and the list works even if you leave your household's names empty.
5. For each sender that needs a request, the app finds the sender's website:
   - A web address printed on the piece is used as is.
   - Otherwise it searches the web (the Brave Search API if you set a key, else DuckDuckGo),
     most specific search first: a phone number printed on
     the piece, then the sender's name with the city from the return address, then the name
     with your own city.
   - A result is only accepted if it can be tied to the sender: the phone number from the
     piece appears on the site, or the site's name matches. If nothing matches, the best guess
     is shown but no address is filled in.
   - Without an API key, DuckDuckGo limits automated searches. When it does, the lookup stops
     and is retried on later runs.

   The app then crawls the site's contact, privacy and opt-out pages for email addresses and
   ranks them.

6. A draft appears in the app's sidebar panel. Nothing is sent until you press Send; it goes out
   over SMTP from the dedicated mailbox.

Mail that never shows up in a digest can be added by hand: "Add photo" lets you take a
picture or choose one you already have, and the picture goes through the same steps as a scan. It is
shrunk in the browser first, which also drops what a phone stores inside a photo, such as where
it was taken. A photo can show more than the mailpiece, so unlike a scan it is only attached
to a request if you tick "Attach your photo" on that request.

Requests only repeat what is printed on the mailpiece: the addressee as the sender wrote it and
your address. They are not signed with a name, and the From line is the bare mailbox address, so
give the dedicated mailbox a neutral address.

The scans never leave your network. The only outbound traffic is the web search for the sender's
name, the crawl of the sender's site, and the mailbox connection.

## Keeping your address off lists

Informed Unsubscribe helps you get off mailing lists once junk mail arrives. I've been using [Virtual Post Mail](https://refer.virtualpostmail.com/l/1KHIYEPRR/vpmmail) (referral link) to make it less likely for my address to get on lists to begin with. For more on digital hygiene I enjoyed [Andrej Karpathy's post](https://karpathy.bearblog.dev/digital-hygiene/)

## Install

You need a USPS [Informed Delivery](https://informeddelivery.usps.com/) account and a spare Gmail
account for the app to read from and send from.

[![Add the repository to your Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Finformedunsubscribe%2Finformed-unsubscribe-homeassistant)

Or add the repository by hand: in Home Assistant, go to Settings → Apps → App store → ⋮ →
Repositories and add `https://github.com/informedunsubscribe/informed-unsubscribe-homeassistant`.

Then install "Informed Unsubscribe" from the app store and follow its
[Documentation tab](informed_unsubscribe/DOCS.md) to set up the mailbox and fill in the
configuration. Updates show up in Home Assistant like any other app update.

## Development

```sh
uv run --no-project --python 3.12 --with-requirements informed_unsubscribe/requirements.txt \
  --with pytest pytest tests

cd informed_unsubscribe
DATA_DIR=./data EMAIL_ADDRESS=... EMAIL_MAILBOX_PASSWORD=... \
  uv run --no-project --python 3.12 --with-requirements requirements.txt python -m app
```

Every option can be set as an upper-cased environment variable. The UI is at
<http://localhost:8099>. The container installs its Python packages from Alpine, so
`requirements.txt` is only for development. Running outside the container needs `tesseract` on your PATH
(`brew install tesseract`).

To try a change on Home Assistant before publishing it, copy the `informed_unsubscribe/` folder to
`/addons/informed_unsubscribe` (with the Samba or SSH app) and delete the `image:` line from that
copy's `config.yaml`, so Home Assistant builds it locally instead of pulling the published image.
Then go to Settings → Apps → App store → ⋮ → Check for updates and install it from the local
apps section.

Pushing to `prod` builds and publishes the images (`.github/workflows/build.yml`), tagged with the
version in `config.yaml`. Bump the version in `config.yaml` and `app/__init__.py` with every change.

## License

[GNU Affero General Public License v3.0](LICENSE).

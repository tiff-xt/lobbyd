# 0006. Signed ticket tokens and offline scanning

**Status:** Accepted

## Context

Tickets are scanned at the door, each must admit exactly once, and venue networks are unreliable at the exact moment thousands of phones arrive.

## Decision

### Token

The QR code contains a compact signed token: ticket id, tournament id, and a signature made with an Ed25519 private key held by the server. Scanners hold only the **public** key, so a stolen scanner cannot forge tickets. The token contains no personal data.

A signature proves a ticket was issued. It does not prove the ticket is still valid or unused, so the scanner also needs state.

### Online scan

```sql
UPDATE tickets
   SET checked_in_at = now()
 WHERE id = :ticket AND status = 'valid' AND checked_in_at IS NULL
RETURNING id;
```

One row returned means admit. No row means already used or void, and the response says which and when. Two scanners racing on the same ticket cannot both get a row.

### Offline scan

- Before doors open, each scanner downloads a manifest: the ids of all valid tickets, which are already checked in, and the public key.
- Offline, the scanner verifies the signature, checks the ticket against its manifest and its own local scans, admits or refuses, and stores the scan with a unique id and the device time.
- When the network returns it uploads its scans. The scan id is the primary key, so uploading the same batch twice is harmless.
- The server applies scans in order of scan time. The earliest scan of a ticket is `admitted`; later ones are recorded as `duplicate` and reported to the organizer.

## The limitation, stated plainly

Two offline scanners cannot know about each other's scans. A copied QR code could be admitted once at each door during an outage. This cannot be fully solved without connectivity; the choice is between refusing everyone while offline and accepting a small, visible risk. This design accepts the risk, bounds it (it only exists while a device is offline, and only across different devices), and makes every occurrence visible after the fact. An organizer who prefers the strict behaviour can disable offline admission.

Device clocks can be wrong. Sync records both the device time and the server receipt time, and flags scans whose device time is implausible: in the future, or more than three days old. A flagged scan is counted from when it reached the server, so a phone set to last year cannot claim to have scanned first.

## As built

The token is `lbd1.<key id>.<ticket id and tournament id, 32 bytes>.<signature>`, base64url, about 145 characters. The demo client's door page verifies it in the browser with WebCrypto and nothing but the manifest. If an uploaded scan is earlier than the admission already recorded, the two swap: the earlier one becomes the admission. Rotation today means one active key; the manifest already carries a list of keys so a second can be added without changing scanners.

## Options not taken

- **Opaque random codes looked up on the server.** Simple, but nothing works offline and anyone can guess-check codes against the API.
- **Rotating time-based codes in an app.** Defeats screenshots, but requires every attendee to install an app. Possible future addition.
- **Shared secret (HMAC).** Every scanner would hold a key that can mint tickets.

## Consequences

- Admission stays fast and keeps working through network failures.
- Voiding a ticket after manifests were downloaded only reaches an offline scanner when it next syncs. Scanners refresh the manifest whenever they have a connection.
- Key rotation needs a key id in the token; included from the start.

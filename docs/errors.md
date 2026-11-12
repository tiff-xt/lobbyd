# Errors

Every error response is an [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) problem document with content type `application/problem+json`:

```json
{
  "type": "https://github.com/tiff-xt/lobbyd/blob/main/docs/errors.md#email_taken",
  "title": "Conflict",
  "status": 409,
  "code": "email_taken",
  "detail": "An account with this email already exists.",
  "instance": "/auth/signup"
}
```

`code` is stable and is what clients should branch on. `detail` is for people and may change. Every response, success or error, carries an `X-Request-ID` header that matches the server's logs.

## Codes

| Code | Status | Meaning |
| --- | --- | --- |
| <a id="validation_error"></a>`validation_error` | 422 | The request body or parameters are not valid. `errors` lists each field and what is wrong with it. Submitted values are never echoed back. |
| <a id="not_authenticated"></a>`not_authenticated` | 401 | No access token was sent. |
| <a id="invalid_token"></a>`invalid_token` | 401 | The access token is malformed, expired, or belongs to an account that no longer exists. |
| <a id="invalid_credentials"></a>`invalid_credentials` | 401 | Email or password is incorrect. The response is the same whether or not the email is registered. |
| <a id="invalid_refresh_token"></a>`invalid_refresh_token` | 401 | The refresh token is unknown, expired, revoked, or was already used. Reusing a token revokes its whole session. |
| <a id="forbidden"></a>`forbidden` | 403 | Signed in, but not allowed to do this. |
| <a id="email_taken"></a>`email_taken` | 409 | An account with this email already exists. |
| <a id="not_found"></a>`not_found` | 404 | No such route or resource. |
| <a id="internal_error"></a>`internal_error` | 500 | Unexpected failure. Details are in the server logs under the request id, never in the response. |
| <a id="invalid_cursor"></a>`invalid_cursor` | 422 | The pagination cursor is malformed. Use the `next_cursor` value exactly as returned. |
| <a id="venue_not_found"></a>`venue_not_found` | 404 | No such venue, or (when creating a tournament) not one you own. |
| <a id="tournament_not_found"></a>`tournament_not_found` | 404 | No such tournament. A draft looks like this to everyone but its organizer. |
| <a id="ticket_type_not_found"></a>`ticket_type_not_found` | 404 | No such ticket type on this tournament. |
| <a id="invalid_dates"></a>`invalid_dates` | 422 | The dates contradict each other: it must end after it starts, last at most 14 days, and open sales before it starts. |
| <a id="venue_required"></a>`venue_required` | 422 | Reserved seating was requested for an online-only tournament. |
| <a id="unknown_section"></a>`unknown_section` | 422 | A section does not belong to the tournament's venue. |
| <a id="tournament_not_editable"></a>`tournament_not_editable` | 409 | The change is only allowed while the tournament is a draft. After publishing, only title and description can change. |
| <a id="tournament_not_publishable"></a>`tournament_not_publishable` | 409 | Publishing needs at least one ticket type and dates that are not in the past. |
| <a id="ticket_type_name_taken"></a>`ticket_type_name_taken` | 409 | The tournament already has a ticket type with that name. |
| <a id="competitor_type_exists"></a>`competitor_type_exists` | 409 | The tournament already has its team slots. |
| <a id="section_already_assigned"></a>`section_already_assigned` | 409 | A section is already sold under another ticket type. |
| <a id="slug_unavailable"></a>`slug_unavailable` | 409 | No free URL could be found for this title. Retry, or change the title. |

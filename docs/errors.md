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
| <a id="idempotency_key_required"></a>`idempotency_key_required` | 400 | The request needs an `Idempotency-Key` header. |
| <a id="idempotency_key_invalid"></a>`idempotency_key_invalid` | 400 | The key must be 8 to 255 printable characters with no spaces. A UUID is ideal. |
| <a id="idempotency_key_reused"></a>`idempotency_key_reused` | 422 | This key was already used for a different request. Generate a new key for each new action. |
| <a id="hold_not_found"></a>`hold_not_found` | 404 | No such hold, or not yours. |
| <a id="order_not_found"></a>`order_not_found` | 404 | No such order, or not yours. |
| <a id="unknown_seat"></a>`unknown_seat` | 422 | A seat is not on sale for this tournament. |
| <a id="unknown_ticket_type"></a>`unknown_ticket_type` | 422 | `items` must name team slots or general admission for this tournament. Reserved seating is bought by seat. |
| <a id="invalid_quantity"></a>`invalid_quantity` | 422 | A team takes exactly one slot. |
| <a id="seats_unavailable"></a>`seats_unavailable` | 409 | At least one seat is held or sold. `seat_ids` lists which. Nothing was held. |
| <a id="sold_out"></a>`sold_out` | 409 | Not enough left of a counted ticket type. `ticket_type_id` says which. Nothing was held. |
| <a id="hold_exists"></a>`hold_exists` | 409 | You already have an open hold for this tournament. Check out or release it first. `hold_id` identifies it when known. |
| <a id="hold_expired"></a>`hold_expired` | 409 | The hold ran out or was released, and its inventory went back on sale. |
| <a id="hold_already_used"></a>`hold_already_used` | 409 | Checkout has already started for this hold. `order_id` identifies the order when known. |
| <a id="sales_not_open"></a>`sales_not_open` | 409 | Sales have not opened yet. `opens_at` says when. |
| <a id="sales_closed"></a>`sales_closed` | 409 | The tournament has ended or is cancelled. |
| <a id="invalid_signature"></a>`invalid_signature` | 400 | A webhook's signature is missing, wrong, or too old. |
| <a id="invalid_webhook"></a>`invalid_webhook` | 400 | A correctly signed webhook whose body could not be understood. |
| <a id="rate_limited"></a>`rate_limited` | 429 | Too many requests. `Retry-After` (and `retry_after` in the body) says how many seconds to wait. |
| <a id="admission_required"></a>`admission_required` | 403 | The sale has a waiting room and the request carried no valid turn. Join the queue; send the token it gives you as `X-Admission-Token`. |
| <a id="overloaded"></a>`overloaded` | 503 | Every database connection stayed busy for several seconds. Nothing was changed; retry after `Retry-After` seconds. Booking requests are safe to retry with the same idempotency key. |
| <a id="team_registration_required"></a>`team_registration_required` | 422 | A team slot was put in a plain hold. Slots are claimed by registering a team. |
| <a id="no_team_slots"></a>`no_team_slots` | 422 | This tournament has no team entry. |
| <a id="wrong_roster_size"></a>`wrong_roster_size` | 422 | The roster does not have exactly as many players as the format asks for. `team_size` says how many. |
| <a id="team_name_taken"></a>`team_name_taken` | 409 | Another team in this tournament has that name (case is ignored). Nothing was held. |
| <a id="player_already_registered"></a>`player_already_registered` | 409 | A listed player is already on another team in this tournament. Nothing was held. |
| <a id="already_registered"></a>`already_registered` | 409 | The caller already captains a team in this tournament. |
| <a id="registration_closed"></a>`registration_closed` | 409 | Team entry closed when the tournament started. |
| <a id="team_not_found"></a>`team_not_found` | 404 | No such team, or not one the caller captains. |
| <a id="tournament_started"></a>`tournament_started` | 409 | A team cannot withdraw once the tournament has started. |
| <a id="slots_available"></a>`slots_available` | 409 | The waitlist is only for a full tournament. Register the team directly. |
| <a id="already_waiting"></a>`already_waiting` | 409 | The caller already has a place on this tournament's waitlist. |
| <a id="not_on_waitlist"></a>`not_on_waitlist` | 404 | The caller has never joined this tournament's waitlist. |

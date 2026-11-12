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

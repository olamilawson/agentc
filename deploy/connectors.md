# Connecting Canva and Higgsfield

Both are used from the Studio screen (Work → Studio). What they make is saved
to the workstream's folder, or stays in your own Canva account. Nothing is
sent to a third person.

What leaves this server: your prompt goes to Higgsfield; a design's title, and
an image you pick, go to Canva.

## Higgsfield

1. In Higgsfield's API console, create an API key. It comes as two values: a
   key ID and a key secret.
2. On the server:

   ```bash
   cd /opt/personal-agent && git pull && ./deploy/setup.sh --key higgsfield
   ```

   It asks for the two values and hides them as you paste.
3. Open Studio and generate an image.

Higgsfield offers a different set of models to each account. The two the agent
calls are in `config/connectors.yaml`:

```yaml
higgsfield:
  endpoints:
    image: higgsfield-ai/soul/v2/standard
    video: wan/v2.7/text-to-video
```

If a run fails with "refused the request" and a 404 or 403, your account does
not have that model. Put a path from your account's API reference in its
place, commit the change, and deploy.

Each generation spends Higgsfield credits. Its API reports no price for a
request, so the Settings and cost screen does not count them.

## Canva

Canva works through your own sign-in, so there are two parts.

1. In Canva's developer portal (canva.com/developers), create an integration.
   - Under Scopes, enable: `design:content:read`, `design:content:write`,
     `design:meta:read`, `asset:read`, `asset:write`.
   - Under Authentication, add this redirect URL exactly:
     `https://ijeoma.pro/connectors/canva/callback`
   - Note the client ID, and generate a client secret.
2. On the server:

   ```bash
   cd /opt/personal-agent && git pull && ./deploy/setup.sh --key canva
   ```

3. Open Studio and press "Sign in to Canva". Canva asks you to approve the
   access, then sends you back. That is done once.

Then Studio can:

- make a design in your account at a size you choose, blank or holding one
  image from the workstream's folder;
- bring a design back to the folder as a PDF, or as one PNG or JPG for each
  page.

Canva's API has no call that shares a design with another person, so sharing
stays something you do in Canva. Filling a brand template with text is a
Canva Enterprise feature and is not built.

The sign-in is kept in the database so you are not asked again. It is
therefore inside the daily backup; the backups are readable by root alone.
"Disconnect" on the Studio screen removes it from this server.

## A restricted client

A workstream marked restricted reaches neither service until you list
`canva` or `higgsfield` under `approved_for_confidential` in
`config/providers.yaml`.

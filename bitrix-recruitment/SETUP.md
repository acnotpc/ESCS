# ESCS Recruitment Bitrix24 pilot
Status: tested source package, not deployed or installed. No live applicant data integrations.
Purpose: a Bitrix24-embedded recruitment workflow overview, accessible only after server-side user verification. This is an installation foundation, not a working applicant dashboard or AI assistant.

## Run and configure
Use Node 22 or later. Run npm test, then npm start.
Host this service on HTTPS. It listens on PORT (default 3000); health endpoint /health.
Provide secrets through the hosting environment:
- BITRIX_APPLICATION_TOKEN: application verification token obtained from the trusted Bitrix24 local-app configuration, never from an arbitrary first visitor.
- BITRIX_ALLOWED_USER_IDS: comma-separated verified Bitrix24 IDs for Andrew, Zoe and Recruitment Administrator. Do not use names as IDs.
The service fails closed until both are configured. No refresh tokens or applicant data are stored.
Portal fixed to escs.bitrix24.com; verify this is the intended portal before installation.

## Register in Bitrix24
An authorised Bitrix24 administrator creates a server-side local application in Developer resources > Other > Local application.
Handler URL: the deployed HTTPS origin followed by /app.
Menu name: ESCS Recruitment.
Choose only the minimum user scope supporting user.current. No CRM, messaging or record-write scopes are needed for this foundation.
The app does not implement a custom installation wizard; leave “Application completes the installation itself” off.
Retain the verification token securely, configure it on the server, and restrict the app's own availability to the intended audience in Bitrix24.
Verify allowed and denied users. Our allowlist is independently enforced server-side.
Frames are restricted to the exact ESCS portal. No ChatGPT Sites login frame is used.

## Before live data can be enabled
Connect the actual Monday board 424237712 and history board 1986634217 with least-privilege server-side credentials and verify their current schemas.
Verify separate mailbox access for jobs@securecareservices.co.uk and info@exclusive-recruitment.co.uk. Connect Jotform with the correct forms and record mapping.
Implement authorised read adapters, pagination, source timestamps, identity matching, per-user access and audit events. Never copy ChatGPT connection tokens into the app.
Test direct applicants, duplicate notifications, collisions, missing sources and reference consent.
Any AI interpretation needs a separate authorised model/API connection; a ChatGPT plugin installation does not supply a backend API credential.
No sending, record changes, automatic reminders or background schedules are implemented here.

Official implementation reference:
https://apidocs.bitrix24.com/local-integrations/serverside-local-app-with-ui.html

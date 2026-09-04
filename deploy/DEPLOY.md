# Deploying to Google Cloud Run

## 1. Create the project and enable APIs

```bash
gcloud projects create travel-ai-agent-2026 --name="Travel AI Voice Agent"
gcloud config set project travel-ai-agent-2026

gcloud services enable \
  aiplatform.googleapis.com \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com
```

## 2. Store every secret in Secret Manager

Never hardcode a real value anywhere in the repo -- including this file.

```bash
for name in GEMINI_API_KEY AMADEUS_ACCESS_TOKEN DB_PASSWORD \
            TWILIO_ACCOUNT_SID TWILIO_AUTH_TOKEN TWILIO_PHONE_NUMBER \
            SENDGRID_API_KEY \
            STRIPE_SECRET_KEY STRIPE_WEBHOOK_SECRET; do
  gcloud secrets create "$name" --replication-policy="automatic"
done

echo -n "your_actual_value" | gcloud secrets versions add GEMINI_API_KEY --data-file=-
# ...repeat for each secret above
```

`STRIPE_WEBHOOK_SECRET` comes from step 4 below -- you'll add its version
after you know the deployed URL.

## 3. Deploy

```bash
gcloud run deploy travel-voice-agent \
  --source . \
  --region us-central1 \
  --timeout=3600 \
  --min-instances=1 \
  --session-affinity \
  --set-env-vars="DB_HOST=10.x.x.x,DB_NAME=travel_db,DB_USER=postgres,AMADEUS_BASE_URL=https://test.api.amadeus.com,CHECKOUT_SUCCESS_URL=https://your-run-url.a.run.app/,CHECKOUT_CANCEL_URL=https://your-run-url.a.run.app/,SENDGRID_FROM_EMAIL=bookings@your-verified-sending-domain.example.com" \
  --set-secrets="GEMINI_API_KEY=GEMINI_API_KEY:latest,AMADEUS_ACCESS_TOKEN=AMADEUS_ACCESS_TOKEN:latest,DB_PASSWORD=DB_PASSWORD:latest,TWILIO_ACCOUNT_SID=TWILIO_ACCOUNT_SID:latest,TWILIO_AUTH_TOKEN=TWILIO_AUTH_TOKEN:latest,TWILIO_PHONE_NUMBER=TWILIO_PHONE_NUMBER:latest,SENDGRID_API_KEY=SENDGRID_API_KEY:latest,STRIPE_SECRET_KEY=STRIPE_SECRET_KEY:latest,STRIPE_WEBHOOK_SECRET=STRIPE_WEBHOOK_SECRET:latest"
```

Notes on the flags that changed from the original draft:

- **No `--allow-unauthenticated`.** This service can place real, billable
  bookings and (once paid) real charges -- don't leave it open to anyone who
  finds the URL. Put a real frontend/auth layer in front of it, or if you
  need a quick public demo, add `--allow-unauthenticated` back deliberately
  and say so to yourself in the commit message.
- **`--session-affinity`** routes a given browser's WebSocket connection back
  to the same instance for the life of that connection. Combined with
  `--min-instances=1` this keeps a single voice session from being split
  across instances. It does **not** solve state loss on a restart or
  autoscale event -- see `PRODUCTION_CHECKLIST.md` on session persistence.
- **`--timeout=3600`** keeps long voice calls from being cut off by Cloud
  Run's default request timeout.

## 4. Wire up the Stripe webhook

```bash
gcloud run services describe travel-voice-agent --region us-central1 --format='value(status.url)'
```

In the Stripe Dashboard (or `stripe webhook_endpoints create`), add an
endpoint at `<that URL>/stripe/webhook` listening for at least:
`checkout.session.completed`, `checkout.session.async_payment_succeeded`,
`checkout.session.async_payment_failed`, `checkout.session.expired`. Copy the
signing secret it gives you into `STRIPE_WEBHOOK_SECRET`:

```bash
echo -n "whsec_..." | gcloud secrets versions add STRIPE_WEBHOOK_SECRET --data-file=-
gcloud run services update travel-voice-agent --region us-central1  # picks up :latest
```

## 5. Verify

```bash
curl https://your-run-url.a.run.app/healthz
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=travel-voice-agent" --limit=20
```

Then open the URL in a browser, place a test booking with Stripe test card
`4242 4242 4242 4242`, and confirm a row appears in both `payments` and
`travel_bookings`.

Before pointing this at real Amadeus/Stripe production keys, read
`PRODUCTION_CHECKLIST.md` end to end.
